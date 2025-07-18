import torch
import numpy as np
from PIL import Image
import folder_paths
import os
import json
import asyncio
import aiohttp
from aiohttp import web
import io
import server
import subprocess
import platform
import time
import atexit
import signal

# Import shared utilities
from .utils.logging import debug_log, log
from .utils.config import CONFIG_FILE, get_default_config, load_config, save_config, ensure_config_exists
from .utils.image import tensor_to_pil, pil_to_tensor, ensure_contiguous
from .utils.process import is_process_alive, terminate_process, get_python_executable
from .utils.network import handle_api_error, get_server_port, get_server_loop, get_client_session, cleanup_client_session
from .utils.async_helpers import run_async_in_server_loop
from .utils.constants import (
    WORKER_JOB_TIMEOUT, PROCESS_TERMINATION_TIMEOUT, WORKER_CHECK_INTERVAL, 
    STATUS_CHECK_INTERVAL, CHUNK_SIZE, LOG_TAIL_BYTES, WORKER_LOG_PATTERN, 
    WORKER_STARTUP_DELAY, PROCESS_WAIT_TIMEOUT, MEMORY_CLEAR_DELAY
)

# Try to import psutil for better process management
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    log("psutil not available, using fallback process management")
    PSUTIL_AVAILABLE = False

# Register cleanup for aiohttp session
def cleanup():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(cleanup_client_session())
    loop.close()

atexit.register(cleanup)

# --- API Endpoints ---
@server.PromptServer.instance.routes.get("/distributed/config")
async def get_config_endpoint(request):
    config = load_config()
    return web.json_response(config)

@server.PromptServer.instance.routes.get("/distributed/queue_status/{job_id}")
async def queue_status_endpoint(request):
    """Check if a job queue is initialized."""
    try:
        job_id = request.match_info['job_id']
        
        # Import to ensure initialization
        from .distributed_upscale import ensure_tile_jobs_initialized
        prompt_server = ensure_tile_jobs_initialized()
        
        async with prompt_server.distributed_tile_jobs_lock:
            exists = job_id in prompt_server.distributed_pending_tile_jobs
        
        debug_log(f"Queue status check for job {job_id}: {'exists' if exists else 'not found'}")
        return web.json_response({"exists": exists, "job_id": job_id})
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/worker/clear_launching")
async def clear_launching_state(request):
    """Clear the launching flag when worker is confirmed running."""
    try:
        data = await request.json()
        worker_id = str(data.get('worker_id'))
        
        if not worker_id:
            return await handle_api_error(request, "worker_id is required", 400)
        
        # Clear launching flag in managed processes
        if worker_id in worker_manager.processes:
            if 'launching' in worker_manager.processes[worker_id]:
                del worker_manager.processes[worker_id]['launching']
                worker_manager.save_processes()
                debug_log(f"Cleared launching state for worker {worker_id}")
        
        return web.json_response({"status": "success"})
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.get("/distributed/network_info")
async def get_network_info_endpoint(request):
    """Get network interfaces and recommend best IP for master."""
    import socket
    
    def get_network_ips():
        """Get all network IPs, trying multiple methods."""
        ips = []
        hostname = socket.gethostname()
        
        # Method 1: Try socket.getaddrinfo
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for info in addr_info:
                ip = info[4][0]
                if ip and ip not in ips and not ip.startswith('::'):  # Skip IPv6 for now
                    ips.append(ip)
        except:
            pass
        
        # Method 2: Try to connect to external server and get local IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # Google DNS
            local_ip = s.getsockname()[0]
            s.close()
            if local_ip not in ips:
                ips.append(local_ip)
        except:
            pass
        
        # Method 3: Platform-specific commands
        
        try:
            if platform.system() == "Windows":
                # Windows ipconfig
                result = subprocess.run(["ipconfig"], capture_output=True, text=True)
                lines = result.stdout.split('\n')
                for i, line in enumerate(lines):
                    if 'IPv4' in line and i + 1 < len(lines):
                        ip = lines[i].split(':')[-1].strip()
                        if ip and ip not in ips:
                            ips.append(ip)
            else:
                # Unix/Linux/Mac ifconfig or ip addr
                try:
                    result = subprocess.run(["ip", "addr"], capture_output=True, text=True)
                except:
                    result = subprocess.run(["ifconfig"], capture_output=True, text=True)
                
                import re
                ip_pattern = re.compile(r'inet\s+(\d+\.\d+\.\d+\.\d+)')
                for match in ip_pattern.finditer(result.stdout):
                    ip = match.group(1)
                    if ip and ip not in ips:
                        ips.append(ip)
        except:
            pass
        
        return ips
    
    def get_recommended_ip(ips):
        """Choose the best IP for master-worker communication."""
        # Priority order:
        # 1. Private network ranges (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
        # 2. Other non-localhost IPs
        # 3. Localhost as last resort
        
        private_ips = []
        public_ips = []
        
        for ip in ips:
            if ip.startswith('127.') or ip == 'localhost':
                continue
            elif (ip.startswith('192.168.') or 
                  ip.startswith('10.') or 
                  (ip.startswith('172.') and 16 <= int(ip.split('.')[1]) <= 31)):
                private_ips.append(ip)
            else:
                public_ips.append(ip)
        
        # Prefer private IPs
        if private_ips:
            # Prefer 192.168 range as it's most common
            for ip in private_ips:
                if ip.startswith('192.168.'):
                    return ip
            return private_ips[0]
        elif public_ips:
            return public_ips[0]
        elif ips:
            return ips[0]
        else:
            return None
    
    try:
        hostname = socket.gethostname()
        all_ips = get_network_ips()
        recommended_ip = get_recommended_ip(all_ips)
        
        return web.json_response({
            "status": "success",
            "hostname": hostname,
            "all_ips": all_ips,
            "recommended_ip": recommended_ip,
            "message": "Auto-detected network configuration"
        })
    except Exception as e:
        return web.json_response({
            "status": "error",
            "message": str(e),
            "hostname": "unknown",
            "all_ips": [],
            "recommended_ip": None
        })

@server.PromptServer.instance.routes.post("/distributed/config/update_worker")
async def update_worker_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if worker_id is None:
            return await handle_api_error(request, "Missing worker_id", 400)
            
        config = load_config()
        worker_found = False
        
        for worker in config.get("workers", []):
            if worker["id"] == worker_id:
                # Update all provided fields
                if "enabled" in data:
                    worker["enabled"] = data["enabled"]
                if "name" in data:
                    worker["name"] = data["name"]
                if "port" in data:
                    worker["port"] = data["port"]
                    
                # Handle host field - remove it if None
                if "host" in data:
                    if data["host"] is None:
                        worker.pop("host", None)
                    else:
                        worker["host"] = data["host"]
                        
                # Handle cuda_device field - remove it if None
                if "cuda_device" in data:
                    if data["cuda_device"] is None:
                        worker.pop("cuda_device", None)
                    else:
                        worker["cuda_device"] = data["cuda_device"]
                        
                # Handle extra_args field - remove it if None
                if "extra_args" in data:
                    if data["extra_args"] is None:
                        worker.pop("extra_args", None)
                    else:
                        worker["extra_args"] = data["extra_args"]
                worker_found = True
                break
                
        if not worker_found:
            # If worker not found and all required fields are provided, create new worker
            if all(key in data for key in ["name", "port", "cuda_device"]):
                new_worker = {
                    "id": worker_id,
                    "name": data["name"],
                    "host": data.get("host", "localhost"),
                    "port": data["port"],
                    "cuda_device": data["cuda_device"],
                    "enabled": data.get("enabled", False),
                    "extra_args": data.get("extra_args", "")
                }
                if "workers" not in config:
                    config["workers"] = []
                config["workers"].append(new_worker)
                worker_found = True
            else:
                return await handle_api_error(request, f"Worker {worker_id} not found and missing required fields for creation", 404)
            
        if save_config(config):
            return web.json_response({"status": "success"})
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/config/delete_worker")
async def delete_worker_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if worker_id is None:
            return await handle_api_error(request, "Missing worker_id", 400)
            
        config = load_config()
        workers = config.get("workers", [])
        
        # Find and remove the worker
        worker_index = -1
        for i, worker in enumerate(workers):
            if worker["id"] == worker_id:
                worker_index = i
                break
                
        if worker_index == -1:
            return await handle_api_error(request, f"Worker {worker_id} not found", 404)
            
        # Remove the worker
        removed_worker = workers.pop(worker_index)
        
        if save_config(config):
            return web.json_response({
                "status": "success",
                "message": f"Worker {removed_worker.get('name', worker_id)} deleted"
            })
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/config/update_setting")
async def update_setting_endpoint(request):
    """Updates a specific key in the settings object."""
    try:
        data = await request.json()
        key = data.get("key")
        value = data.get("value")

        if not key or value is None:
            return await handle_api_error(request, "Missing 'key' or 'value' in request", 400)

        config = load_config()
        if 'settings' not in config:
            config['settings'] = {}
        
        config['settings'][key] = value

        if save_config(config):
            return web.json_response({"status": "success", "message": f"Setting '{key}' updated."})
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/config/update_master")
async def update_master_endpoint(request):
    """Updates master configuration."""
    try:
        data = await request.json()
        
        config = load_config()
        if 'master' not in config:
            config['master'] = {}
        
        # Update all provided fields
        if "host" in data:
            config['master']['host'] = data['host']
        if "port" in data:
            config['master']['port'] = data['port']
        if "cuda_device" in data:
            config['master']['cuda_device'] = data['cuda_device']
        if "extra_args" in data:
            config['master']['extra_args'] = data['extra_args']
            
        if save_config(config):
            return web.json_response({"status": "success", "message": "Master configuration updated."})
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/prepare_job")
async def prepare_job_endpoint(request):
    try:
        data = await request.json()
        multi_job_id = data.get('multi_job_id')
        if not multi_job_id:
            return await handle_api_error(request, "Missing multi_job_id", 400)

        async with prompt_server.distributed_jobs_lock:
            if multi_job_id not in prompt_server.distributed_pending_jobs:
                prompt_server.distributed_pending_jobs[multi_job_id] = asyncio.Queue()
        
        debug_log(f"Prepared queue for job {multi_job_id}")
        return web.json_response({"status": "success"})
    except Exception as e:
        return await handle_api_error(request, e)

@server.PromptServer.instance.routes.post("/distributed/clear_memory")
async def clear_memory_endpoint(request):
    debug_log("Received request to clear VRAM.")
    try:
        # Use ComfyUI's prompt server queue system like the /free endpoint does
        if hasattr(server.PromptServer.instance, 'prompt_queue'):
            server.PromptServer.instance.prompt_queue.set_flag("unload_models", True)
            server.PromptServer.instance.prompt_queue.set_flag("free_memory", True)
            debug_log("Set queue flags for memory clearing.")
        
        # Wait a bit for the queue to process
        await asyncio.sleep(MEMORY_CLEAR_DELAY)
        
        # Also do direct cleanup as backup, but with error handling
        import gc
        import comfy.model_management as mm
        
        try:
            mm.unload_all_models()
        except AttributeError as e:
            debug_log(f"Warning during model unload: {e}")
        
        try:
            mm.soft_empty_cache()
        except Exception as e:
            debug_log(f"Warning during cache clear: {e}")
        
        for _ in range(3):
            gc.collect()
        
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        
        debug_log("VRAM cleared successfully.")
        return web.json_response({"status": "success", "message": "GPU memory cleared."})
    except Exception as e:
        # Even if there's an error, try to do basic cleanup
        import gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        debug_log(f"Partial VRAM clear completed with warning: {e}")
        return web.json_response({"status": "success", "message": "GPU memory cleared (with warnings)"})


@server.PromptServer.instance.routes.post("/distributed/launch_worker")
async def launch_worker_endpoint(request):
    """Launch a worker process from the UI."""
    try:
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if not worker_id:
            return await handle_api_error(request, "Missing worker_id", 400)
        
        # Find worker config
        config = load_config()
        worker = next((w for w in config.get("workers", []) if w["id"] == worker_id), None)
        if not worker:
            return await handle_api_error(request, f"Worker {worker_id} not found", 404)
        
        # Ensure consistent string ID
        worker_id_str = str(worker_id)
        
        # Check if already running (managed by this instance)
        if worker_id_str in worker_manager.processes:
            proc_info = worker_manager.processes[worker_id_str]
            process = proc_info.get('process')
            
            # Check if still running
            is_running = False
            if process:
                is_running = process.poll() is None
            else:
                # Restored process without subprocess object
                is_running = worker_manager._is_process_running(proc_info['pid'])
            
            if is_running:
                return web.json_response({
                    "status": "error",
                    "message": "Worker already running (managed by UI)",
                    "pid": proc_info['pid'],
                    "log_file": proc_info.get('log_file')
                }, status=409)
            else:
                # Process is dead, remove it
                del worker_manager.processes[worker_id_str]
                worker_manager.save_processes()
        
        # Launch the worker
        try:
            pid = worker_manager.launch_worker(worker)
            log_file = worker_manager.processes[worker_id_str].get('log_file')
            return web.json_response({
                "status": "success",
                "pid": pid,
                "message": f"Worker {worker['name']} launched",
                "log_file": log_file
            })
        except Exception as e:
            return await handle_api_error(request, f"Failed to launch worker: {str(e)}", 500)
            
    except Exception as e:
        return await handle_api_error(request, e, 400)


@server.PromptServer.instance.routes.post("/distributed/stop_worker")
async def stop_worker_endpoint(request):
    """Stop a worker process that was launched from the UI."""
    try:
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if not worker_id:
            return await handle_api_error(request, "Missing worker_id", 400)
        
        success, message = worker_manager.stop_worker(worker_id)
        
        if success:
            return web.json_response({"status": "success", "message": message})
        else:
            return web.json_response({"status": "error", "message": message}, 
                                   status=404 if "not managed" in message else 409)
            
    except Exception as e:
        return await handle_api_error(request, e, 400)


@server.PromptServer.instance.routes.get("/distributed/managed_workers")
async def get_managed_workers_endpoint(request):
    """Get list of workers managed by this UI instance."""
    try:
        managed = worker_manager.get_managed_workers()
        return web.json_response({
            "status": "success",
            "managed_workers": managed
        })
    except Exception as e:
        return await handle_api_error(request, e, 500)


@server.PromptServer.instance.routes.get("/distributed/worker_log/{worker_id}")
async def get_worker_log_endpoint(request):
    """Get log content for a specific worker."""
    try:
        worker_id = request.match_info['worker_id']
        
        # Ensure worker_id is string
        worker_id = str(worker_id)
        
        # Check if we manage this worker
        if worker_id not in worker_manager.processes:
            return await handle_api_error(request, f"Worker {worker_id} not managed by UI", 404)
        
        proc_info = worker_manager.processes[worker_id]
        log_file = proc_info.get('log_file')
        
        if not log_file or not os.path.exists(log_file):
            return await handle_api_error(request, "Log file not found", 404)
        
        # Read last N lines (or full file if small)
        lines_to_read = int(request.query.get('lines', 1000))
        
        try:
            # Get file size
            file_size = os.path.getsize(log_file)
            
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                if lines_to_read > 0 and file_size > 1024 * 1024:  # If file > 1MB and limited lines requested
                    # Read last N lines efficiently
                    lines = []
                    # Start from end and work backwards
                    f.seek(0, 2)  # Go to end
                    file_length = f.tell()
                    
                    # Read chunks from end
                    chunk_size = min(CHUNK_SIZE, file_length)
                    while len(lines) < lines_to_read and f.tell() > 0:
                        # Move back and read chunk
                        current_pos = max(0, f.tell() - chunk_size)
                        f.seek(current_pos)
                        chunk = f.read(chunk_size)
                        
                        # Process chunk
                        chunk_lines = chunk.splitlines()
                        if current_pos > 0:
                            # Partial line at beginning, combine with next chunk
                            chunk_lines = chunk_lines[1:]
                        
                        lines = chunk_lines + lines
                        
                        # Move back for next chunk
                        f.seek(current_pos)
                    
                    # Take only last N lines
                    content = '\n'.join(lines[-lines_to_read:])
                    truncated = len(lines) > lines_to_read
                else:
                    # Read entire file
                    content = f.read()
                    truncated = False
            
            return web.json_response({
                "status": "success",
                "content": content,
                "log_file": log_file,
                "file_size": file_size,
                "truncated": truncated,
                "lines_shown": lines_to_read if truncated else content.count('\n') + 1
            })
            
        except Exception as e:
            return await handle_api_error(request, f"Error reading log file: {str(e)}", 500)
            
    except Exception as e:
        return await handle_api_error(request, e, 500)


# --- Worker Process Management ---
class WorkerProcessManager:
    def __init__(self):
        self.processes = {}  # worker_id -> process info
        self.load_processes()
        
    def find_comfy_root(self):
        """Find the ComfyUI root directory."""
        # Start from current file location and go up
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # This file is in ComfyUI/custom_nodes/ComfyUI-Distributed/
        # So go up two levels to get to ComfyUI root
        comfy_root = os.path.dirname(os.path.dirname(current_dir))
        return comfy_root
        
    def _find_windows_terminal(self):
        """Find Windows Terminal executable."""
        # Common locations for Windows Terminal
        possible_paths = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\WindowsApps\Microsoft.WindowsTerminal_*\wt.exe"),
            "wt.exe"  # Try PATH
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                return path
            # Handle wildcard for WindowsApps
            if '*' in path:
                import glob
                matches = glob.glob(path)
                if matches:
                    return matches[0]
        
        # Try to find it in PATH
        import shutil
        wt_path = shutil.which("wt")
        if wt_path:
            return wt_path
            
        return None
        
    def build_launch_command(self, worker_config, comfy_root):
        """Build the command to launch a worker."""
        # Use main.py directly - it's the most reliable method
        main_py = os.path.join(comfy_root, "main.py")
        
        if os.path.exists(main_py):
            cmd = [
                get_python_executable(),
                main_py,
                "--port", str(worker_config['port']),
                "--enable-cors-header"
            ]
            debug_log(f"Using main.py: {main_py}")
        else:
            # Fallback error
            raise RuntimeError(f"Could not find main.py in {comfy_root}")
        
        # Add any extra arguments
        if worker_config.get('extra_args'):
            cmd.extend(worker_config['extra_args'].split())
            
        return cmd
        
    def launch_worker(self, worker_config, show_window=False):
        """Launch a worker process with logging."""
        comfy_root = self.find_comfy_root()
        
        # Set up environment
        env = os.environ.copy()
        env['CUDA_VISIBLE_DEVICES'] = str(worker_config.get('cuda_device', 0))
        env['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
        
        # Pass master PID to worker so it can monitor if master is still alive
        env['COMFYUI_MASTER_PID'] = str(os.getpid())
        
        cmd = self.build_launch_command(worker_config, comfy_root)
        
        # Change to ComfyUI root directory for the process
        cwd = comfy_root
        
        # Create log directory and file
        log_dir = os.path.join(comfy_root, "logs", "workers")
        os.makedirs(log_dir, exist_ok=True)
        
        # Use daily log files instead of timestamp
        date_stamp = time.strftime("%Y%m%d")
        worker_name = worker_config.get('name', f'Worker{worker_config["id"]}')
        # Clean worker name for filename
        safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in worker_name)
        log_file = os.path.join(log_dir, f"{safe_name}_{date_stamp}.log")
        
        # Launch process with logging (append mode for daily logs)
        with open(log_file, 'a') as log_handle:
            # Write startup info to log with timestamp
            log_handle.write(f"\n\n{'='*50}\n")
            log_handle.write(f"=== ComfyUI Worker Session Started ===\n")
            log_handle.write(f"Worker: {worker_name}\n")
            log_handle.write(f"Port: {worker_config['port']}\n")
            log_handle.write(f"CUDA Device: {worker_config.get('cuda_device', 0)}\n")
            log_handle.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_handle.write(f"Command: {' '.join(cmd)}\n")
            
            # Note about worker behavior
            config = load_config()
            stop_on_master_exit = config.get('settings', {}).get('stop_workers_on_master_exit', True)
            
            if stop_on_master_exit:
                log_handle.write("Note: Worker will stop when master shuts down\n")
            else:
                log_handle.write("Note: Worker will continue running after master shuts down\n")
            
            log_handle.write("=" * 30 + "\n\n")
            log_handle.flush()
            
            # Wrap command with monitor if needed
            if stop_on_master_exit and env.get('COMFYUI_MASTER_PID'):
                # Use the monitor wrapper
                monitor_script = os.path.join(os.path.dirname(__file__), 'worker_monitor.py')
                monitored_cmd = [get_python_executable(), monitor_script] + cmd
                log_handle.write(f"[Worker Monitor] Monitoring master PID: {env['COMFYUI_MASTER_PID']}\n")
                log_handle.flush()
            else:
                monitored_cmd = cmd
            
            # Platform-specific process creation - always hidden with logging
            if platform.system() == "Windows":
                CREATE_NO_WINDOW = 0x08000000
                process = subprocess.Popen(
                    monitored_cmd, env=env, cwd=cwd,
                    stdout=log_handle, 
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW
                )
            else:
                # Unix-like systems
                process = subprocess.Popen(
                    monitored_cmd, env=env, cwd=cwd,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True  # Detach from parent
                )
        
        # Track the process with log file info - use string ID for consistency
        worker_id = str(worker_config['id'])
        self.processes[worker_id] = {
            'pid': process.pid,
            'process': process,
            'started_at': time.time(),
            'config': worker_config,
            'log_file': log_file,
            'is_monitor': stop_on_master_exit and env.get('COMFYUI_MASTER_PID'),  # Track if using monitor
            'launching': True  # Mark as launching until confirmed running
        }
        
        # Save process info for persistence
        self.save_processes()
        
        if stop_on_master_exit and env.get('COMFYUI_MASTER_PID'):
            debug_log(f"Launched worker {worker_name} via monitor (Monitor PID: {process.pid})")
        else:
            log(f"Launched worker {worker_name} directly (PID: {process.pid})")
        debug_log(f"Log file: {log_file}")
        return process.pid
        
    def stop_worker(self, worker_id):
        """Stop a worker process."""
        # Ensure worker_id is string
        worker_id = str(worker_id)
        if worker_id not in self.processes:
            return False, "Worker not managed by UI"
            
        proc_info = self.processes[worker_id]
        process = proc_info.get('process')
        pid = proc_info['pid']
        
        debug_log(f"Attempting to stop worker {worker_id} (PID: {pid})")
        
        # For restored processes without subprocess object
        if not process:
            try:
                print(f"[Distributed] Stopping restored process (no subprocess object)")
                if self._kill_process_tree(pid):
                    del self.processes[worker_id]
                    self.save_processes()
                    debug_log(f"Successfully stopped worker {worker_id} and all child processes")
                    return True, "Worker stopped"
                else:
                    return False, "Failed to stop worker process"
            except Exception as e:
                print(f"[MultiGPU] Exception during stop: {e}")
                return False, f"Error stopping worker: {str(e)}"
        
        # Normal case with subprocess object
        # Check if still running
        if process.poll() is not None:
            # Already stopped
            print(f"[Distributed] Worker {worker_id} already stopped")
            del self.processes[worker_id]
            self.save_processes()
            return False, "Worker already stopped"
            
        # Try to kill the entire process tree
        try:
            debug_log(f"Using process tree kill for worker {worker_id}")
            if self._kill_process_tree(pid):
                # Clean up tracking
                del self.processes[worker_id]
                self.save_processes()
                debug_log(f"Successfully stopped worker {worker_id} and all child processes")
                return True, "Worker stopped"
            else:
                # Fallback to normal termination
                print(f"[Distributed] Process tree kill failed, trying normal termination")
                if process:
                    terminate_process(process, timeout=PROCESS_TERMINATION_TIMEOUT)
                
                del self.processes[worker_id]
                self.save_processes()
                return True, "Worker stopped (fallback)"
                
        except Exception as e:
            print(f"[Distributed] Exception during stop: {e}")
            return False, f"Error stopping worker: {str(e)}"
            
    def get_managed_workers(self):
        """Get list of workers managed by this process."""
        managed = {}
        for worker_id, proc_info in list(self.processes.items()):
            # Check if process is still running
            is_running, _ = self._check_worker_process(worker_id, proc_info)
            
            if is_running:
                managed[worker_id] = {
                    'pid': proc_info['pid'],
                    'started_at': proc_info['started_at'],
                    'log_file': proc_info.get('log_file'),
                    'launching': proc_info.get('launching', False)
                }
            else:
                # Process has stopped, remove from tracking
                del self.processes[worker_id]
        
        return managed
        
    def cleanup_all(self):
        """Stop all managed workers (called on shutdown)."""
        for worker_id in list(self.processes.keys()):
            try:
                self.stop_worker(worker_id)
            except Exception as e:
                print(f"[Distributed] Error stopping worker {worker_id}: {e}")
        
        # Clear all managed processes from config
        config = load_config()
        config['managed_processes'] = {}
        save_config(config)
    
    def load_processes(self):
        """Load persisted process information from config."""
        config = load_config()
        managed_processes = config.get('managed_processes', {})
        
        # Verify each saved process is still running
        for worker_id, proc_info in managed_processes.items():
            pid = proc_info.get('pid')
            if pid and self._is_process_running(pid):
                # Reconstruct process info
                self.processes[worker_id] = {
                    'pid': pid,
                    'process': None,  # Can't reconstruct subprocess object
                    'started_at': proc_info.get('started_at'),
                    'config': proc_info.get('config'),
                    'log_file': proc_info.get('log_file')
                }
                print(f"[Distributed] Restored worker {worker_id} (PID: {pid})")
            else:
                if pid:
                    print(f"[Distributed] Worker {worker_id} (PID: {pid}) is no longer running")
    
    def save_processes(self):
        """Save process information to config."""
        config = load_config()
        
        # Create serializable version of process info
        managed_processes = {}
        for worker_id, proc_info in self.processes.items():
            # Only save if process is running
            is_running, _ = self._check_worker_process(worker_id, proc_info)
            
            if is_running:
                managed_processes[worker_id] = {
                    'pid': proc_info['pid'],
                    'started_at': proc_info['started_at'],
                    'config': proc_info['config'],
                    'log_file': proc_info.get('log_file'),
                    'launching': proc_info.get('launching', False)
                }
        
        # Update config with managed processes
        config['managed_processes'] = managed_processes
        save_config(config)
    
    def _is_process_running(self, pid):
        """Check if a process with given PID is running."""
        return is_process_alive(pid)
    
    def _check_worker_process(self, worker_id, proc_info):
        """Check if a worker process is still running and return status.
        
        Returns:
            tuple: (is_running, has_subprocess_object)
        """
        process = proc_info.get('process')
        pid = proc_info.get('pid')
        
        if process:
            # Normal case with subprocess object
            return process.poll() is None, True
        elif pid:
            # Restored process without subprocess object
            return self._is_process_running(pid), False
        else:
            # No process or PID
            return False, False
    
    def _kill_process_tree(self, pid):
        """Kill a process and all its children."""
        if PSUTIL_AVAILABLE:
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                
                # Log what we're about to kill
                debug_log(f"Killing process tree for PID {pid} ({parent.name()})")
                for child in children:
                    debug_log(f"  - Child PID {child.pid} ({child.name()})")
                
                # Kill children first
                for child in children:
                    try:
                        debug_log(f"Terminating child {child.pid}")
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                
                # Wait a bit for graceful termination
                gone, alive = psutil.wait_procs(children, timeout=PROCESS_WAIT_TIMEOUT)
                
                # Force kill any remaining
                for child in alive:
                    try:
                        debug_log(f"Force killing child {child.pid}")
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                
                # Finally kill the parent
                try:
                    debug_log(f"Terminating parent {pid}")
                    parent.terminate()
                    parent.wait(timeout=PROCESS_WAIT_TIMEOUT)
                except psutil.TimeoutExpired:
                    debug_log(f"Force killing parent {pid}")
                    parent.kill()
                except psutil.NoSuchProcess:
                    debug_log(f"Parent process {pid} already gone")
                    
                return True
                
            except psutil.NoSuchProcess:
                debug_log(f"Process {pid} does not exist")
                return False
            except Exception as e:
                debug_log(f"Error killing process tree: {e}")
                # Fall through to OS commands
        
        # Fallback to OS-specific commands
        print(f"[Distributed] Using OS commands to kill process tree")
        if platform.system() == "Windows":
            try:
                # Use wmic to find child processes
                result = subprocess.run(['wmic', 'process', 'where', f'ParentProcessId={pid}', 'get', 'ProcessId'], 
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')[1:]  # Skip header
                    child_pids = [line.strip() for line in lines if line.strip() and line.strip().isdigit()]
                    
                    print(f"[Distributed] Found child processes: {child_pids}")
                    
                    # Kill each child
                    for child_pid in child_pids:
                        try:
                            subprocess.run(['taskkill', '/F', '/PID', child_pid], 
                                         capture_output=True, check=False)
                        except:
                            pass
                
                # Kill the parent with tree flag
                result = subprocess.run(['taskkill', '/F', '/PID', str(pid), '/T'], 
                                      capture_output=True, text=True)
                print(f"[Distributed] Taskkill result: {result.stdout.strip()}")
                return result.returncode == 0
            except Exception as e:
                print(f"[Distributed] Error with taskkill: {e}")
                return False
        else:
            # Unix: use pkill
            try:
                subprocess.run(['pkill', '-TERM', '-P', str(pid)], check=False)
                time.sleep(WORKER_CHECK_INTERVAL)
                subprocess.run(['pkill', '-KILL', '-P', str(pid)], check=False)
                os.kill(pid, signal.SIGKILL)
                return True
            except:
                return False

# Create global instance
worker_manager = WorkerProcessManager()

# Auto-launch workers if enabled
def auto_launch_workers():
    """Launch enabled workers if auto_launch_workers is set to true."""
    try:
        config = load_config()
        if config.get('settings', {}).get('auto_launch_workers', False):
            log("Auto-launch workers is enabled, checking for workers to start...")
            
            # Clear managed_processes before launching new workers
            # This handles cases where the master was killed without proper cleanup
            if config.get('managed_processes'):
                log("Clearing old managed_processes before auto-launch...")
                config['managed_processes'] = {}
                save_config(config)
            
            workers = config.get('workers', [])
            launched_count = 0
            
            for worker in workers:
                if worker.get('enabled', False):
                    worker_id = worker.get('id')
                    worker_name = worker.get('name', f'Worker {worker_id}')
                    
                    # Skip remote workers
                    host = worker.get('host', 'localhost').lower()
                    if host not in ['localhost', '127.0.0.1', '', None]:
                        debug_log(f"Skipping remote worker {worker_name} (host: {host})")
                        continue
                    
                    # Check if already running
                    if str(worker_id) in worker_manager.processes:
                        proc_info = worker_manager.processes[str(worker_id)]
                        if worker_manager._is_process_running(proc_info['pid']):
                            debug_log(f"Worker {worker_name} already running, skipping")
                            continue
                    
                    # Launch the worker
                    try:
                        pid = worker_manager.launch_worker(worker)
                        log(f"Auto-launched worker {worker_name} (PID: {pid})")
                        
                        # Mark as launching in managed processes
                        if str(worker_id) in worker_manager.processes:
                            worker_manager.processes[str(worker_id)]['launching'] = True
                            worker_manager.save_processes()
                        
                        launched_count += 1
                    except Exception as e:
                        log(f"Failed to auto-launch worker {worker_name}: {e}")
            
            if launched_count > 0:
                log(f"Auto-launched {launched_count} worker(s)")
            else:
                debug_log("No workers to auto-launch")
        else:
            debug_log("Auto-launch workers is disabled")
    except Exception as e:
        log(f"Error during auto-launch: {e}")

# Schedule auto-launch after a short delay to ensure server is ready
def delayed_auto_launch():
    """Delay auto-launch to ensure server is fully initialized."""
    import threading
    timer = threading.Timer(WORKER_STARTUP_DELAY, auto_launch_workers)
    timer.daemon = True
    timer.start()

# Call delayed auto-launch only if we're the master (not a worker)
if not os.environ.get('COMFYUI_MASTER_PID'):
    delayed_auto_launch()
else:
    debug_log("Running as worker, skipping auto-launch")

# Register cleanup on exit - only clean up if setting is enabled
def cleanup_on_exit(signum=None, frame=None):
    """Handle cleanup on exit or signal"""
    try:
        config = load_config()
        if config.get('settings', {}).get('stop_workers_on_master_exit', True):
            print("\n[Distributed] Master shutting down, stopping all managed workers...")
            worker_manager.cleanup_all()
        else:
            print("\n[Distributed] Master shutting down, workers will continue running")
            # Still save the current state
            worker_manager.save_processes()
    except Exception as e:
        print(f"[Distributed] Error during cleanup: {e}")

# Register cleanup handlers
atexit.register(cleanup_on_exit)

# Handle terminal window closing and Ctrl+C
try:
    signal.signal(signal.SIGINT, cleanup_on_exit)
    signal.signal(signal.SIGTERM, cleanup_on_exit)
    
    if platform.system() != "Windows":
        # SIGHUP is sent when terminal closes on Unix
        signal.signal(signal.SIGHUP, cleanup_on_exit)
except Exception as e:
    print(f"[Distributed] Warning: Could not set signal handlers: {e}")

# --- Persistent State Storage ---
# Store job queue on the persistent server instance to survive script reloads
prompt_server = server.PromptServer.instance

# Initialize persistent state if not already present
if not hasattr(prompt_server, 'distributed_pending_jobs'):
    debug_log("Initializing persistent job queue on server instance.")
    prompt_server.distributed_pending_jobs = {}
    prompt_server.distributed_jobs_lock = asyncio.Lock()

@server.PromptServer.instance.routes.post("/distributed/load_image")
async def load_image_endpoint(request):
    """Load an image file and return it as base64 data."""
    try:
        data = await request.json()
        image_path = data.get("image_path")
        
        if not image_path:
            return await handle_api_error(request, "Missing image_path", 400)
        
        import folder_paths
        import base64
        from PIL import Image
        import io
        
        # Use ComfyUI's folder paths to find the image
        full_path = folder_paths.get_annotated_filepath(image_path)
        
        if not os.path.exists(full_path):
            return await handle_api_error(request, f"Image not found: {image_path}", 404)
        
        # Load and convert to base64
        with Image.open(full_path) as img:
            # Convert to RGB if needed
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            
            # Save to bytes
            buffer = io.BytesIO()
            img.save(buffer, format='PNG', compress_level=1)  # Fast compression
            img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        return web.json_response({
            "status": "success",
            "image_data": f"data:image/png;base64,{img_base64}"
        })
        
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/job_complete")
async def job_complete_endpoint(request):
    try:
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        image_file = data.get('image')
        worker_id = data.get('worker_id')
        image_index = data.get('image_index')
        is_last = data.get('is_last', 'False').lower() == 'true'

        debug_log(f"job_complete received - job_id: {multi_job_id}, worker: {worker_id}, index: {image_index}, is_last: {is_last}")

        if not all([multi_job_id, image_file]):
            return await handle_api_error(request, "Missing job_id or image data", 400)

        # Process image with error handling
        try:
            img_data = image_file.file.read()
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            # Convert to tensor using utility function
            tensor = pil_to_tensor(img)
            # Ensure tensor is contiguous
            tensor = ensure_contiguous(tensor)
        except Exception as e:
            log(f"Error processing image from worker {worker_id}: {e}")
            return await handle_api_error(request, f"Image processing error: {e}", 400)

        async with prompt_server.distributed_jobs_lock:
            debug_log(f"Current pending jobs: {list(prompt_server.distributed_pending_jobs.keys())}")
            if multi_job_id in prompt_server.distributed_pending_jobs:
                await prompt_server.distributed_pending_jobs[multi_job_id].put({
                    'tensor': tensor,
                    'worker_id': worker_id,
                    'image_index': int(image_index) if image_index else 0,
                    'is_last': is_last
                })
                debug_log(f"Received result for job {multi_job_id} from worker {worker_id} (last: {is_last})")
                debug_log(f"Queue size after put: {prompt_server.distributed_pending_jobs[multi_job_id].qsize()}")
                return web.json_response({"status": "success"})
            else:
                log(f"ERROR: Job {multi_job_id} not found in distributed_pending_jobs")
                return await handle_api_error(request, "Job not found or already complete", 404)
    except Exception as e:
        return await handle_api_error(request, e)


# --- Collector Node ---
class DistributedCollectorNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": { "images": ("IMAGE",) },
            "hidden": {
                "multi_job_id": ("STRING", {"default": ""}),
                "is_worker": ("BOOLEAN", {"default": False}),
                "master_url": ("STRING", {"default": ""}),
                "enabled_worker_ids": ("STRING", {"default": "[]"}),
                "worker_batch_size": ("INT", {"default": 1, "min": 1, "max": 1024}),
                "worker_id": ("STRING", {"default": ""}),
                "pass_through": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "image"
    
    def run(self, images, multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", worker_batch_size=1, worker_id="", pass_through=False):
        if not multi_job_id or pass_through:
            if pass_through:
                print(f"[Distributed Collector] Pass-through mode enabled, returning images unchanged")
            return (images,)

        # Use async helper to run in server loop
        result = run_async_in_server_loop(
            self.execute(images, multi_job_id, is_worker, master_url, enabled_worker_ids, worker_batch_size, worker_id)
        )
        return result

    async def send_image_to_master(self, image_tensor, multi_job_id, master_url, image_index, worker_id, is_last=False):
        """Helper method to send a single image to the master"""
        # Ensure we handle the tensor shape correctly (H, W, C)
        if image_tensor.dim() == 4:  # Has batch dimension
            img = tensor_to_pil(image_tensor, 0)
        else:  # Single image without batch dimension
            # Add batch dimension temporarily for tensor_to_pil
            img = tensor_to_pil(image_tensor.unsqueeze(0), 0)
        byte_io = io.BytesIO()
        # Use PNG with no compression for lossless transfer
        img.save(byte_io, format='PNG', compress_level=0)
        byte_io.seek(0)
        
        data = aiohttp.FormData()
        data.add_field('multi_job_id', multi_job_id)
        data.add_field('worker_id', str(worker_id))
        data.add_field('image_index', str(image_index))
        data.add_field('is_last', str(is_last))
        data.add_field('image', byte_io, filename=f'image_{image_index}.png', content_type='image/png')

        try:
            session = await get_client_session()
            async with session.post(f"{master_url}/distributed/job_complete", data=data) as response:
                response.raise_for_status()
        except Exception as e:
            log(f"Worker - Failed to send image {image_index+1} to master: {e}")

    async def execute(self, images, multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", worker_batch_size=1, worker_id=""):
        if is_worker:
            # Worker mode: send images to master
            debug_log(f"Worker - Job {multi_job_id} complete. Sending {images.shape[0]} image(s) to master")
            
            # Send all images, marking the last one
            for i in range(images.shape[0]):
                is_last = (i == images.shape[0] - 1)
                await self.send_image_to_master(images[i], multi_job_id, master_url, i, worker_id, is_last)
            
            return (images,)
        else:
            # Master mode: collect images from workers
            enabled_workers = json.loads(enabled_worker_ids)
            num_workers = len(enabled_workers)
            if num_workers == 0:
                return (images,)
            
            images_on_cpu = images.cpu()
            master_batch_size = images.shape[0]
            debug_log(f"Master - Job {multi_job_id}: Master has {master_batch_size} images, collecting from {num_workers} workers...")
            
            # Ensure master images are contiguous
            images_on_cpu = ensure_contiguous(images_on_cpu)
            
            # Debug: Check master tensor properties
            debug_log(f"Master tensor - shape: {images_on_cpu.shape}, dtype: {images_on_cpu.dtype}, device: {images_on_cpu.device}")
            
            # Initialize storage for collected images
            worker_images = {}  # Dict to store images by worker_id and index
            
            # Get the existing queue - it should already exist from prepare_job
            async with prompt_server.distributed_jobs_lock:
                if multi_job_id not in prompt_server.distributed_pending_jobs:
                    log(f"Master - WARNING: Queue doesn't exist for job {multi_job_id}, creating one")
                    prompt_server.distributed_pending_jobs[multi_job_id] = asyncio.Queue()
                else:
                    existing_size = prompt_server.distributed_pending_jobs[multi_job_id].qsize()
                    debug_log(f"Master - Using existing queue for job {multi_job_id} (current size: {existing_size})")
            
            # Collect images until all workers report they're done
            collected_count = 0
            workers_done = set()
            
            # Use a reasonable timeout for the first image
            timeout = WORKER_JOB_TIMEOUT
            
            debug_log(f"Master - Starting collection loop, expecting {num_workers} workers")
            
            # Get queue size before starting
            async with prompt_server.distributed_jobs_lock:
                q = prompt_server.distributed_pending_jobs[multi_job_id]
                initial_size = q.qsize()
            debug_log(f"Master - Queue size before collection: {initial_size}")
            
            while len(workers_done) < num_workers:
                try:
                    # Get the queue again each time to ensure we have the right reference
                    async with prompt_server.distributed_jobs_lock:
                        q = prompt_server.distributed_pending_jobs[multi_job_id]
                        current_size = q.qsize()
                    
                    debug_log(f"Master - Waiting for queue item, timeout={timeout}s, queue size={current_size}")
                    
                    result = await asyncio.wait_for(q.get(), timeout=timeout)
                    worker_id = result['worker_id']
                    image_index = result['image_index']
                    tensor = result['tensor']
                    is_last = result.get('is_last', False)
                    
                    debug_log(f"Master - Got result from worker {worker_id}, image {image_index}, is_last={is_last}")
                    
                    if worker_id not in worker_images:
                        worker_images[worker_id] = {}
                    worker_images[worker_id][image_index] = tensor
                    
                    # Debug: Check worker tensor properties
                    if collected_count == 0:  # Only print once
                        debug_log(f"Master - Worker tensor - shape: {tensor.shape}, dtype: {tensor.dtype}, device: {tensor.device}")
                    
                    collected_count += 1
                    
                    # Once we start receiving images, use shorter timeout
                    timeout = WORKER_JOB_TIMEOUT
                    
                    if is_last:
                        workers_done.add(worker_id)
                        debug_log(f"Master - Worker {worker_id} done. Collected {len(worker_images[worker_id])} images")
                    else:
                        debug_log(f"Master - Collected image {image_index + 1} from worker {worker_id}")
                    
                except asyncio.TimeoutError:
                    missing_workers = set(str(w) for w in enabled_workers) - workers_done
                    log(f"Master - Timeout. Still waiting for workers: {list(missing_workers)}")
                    
                    # Check queue size again with lock
                    async with prompt_server.distributed_jobs_lock:
                        if multi_job_id in prompt_server.distributed_pending_jobs:
                            final_q = prompt_server.distributed_pending_jobs[multi_job_id]
                            final_size = final_q.qsize()
                            debug_log(f"Master - Queue size at timeout: {final_size}")
                            
                            # Try to drain any remaining items
                            remaining_items = []
                            while not final_q.empty():
                                try:
                                    item = final_q.get_nowait()
                                    remaining_items.append(item)
                                except asyncio.QueueEmpty:
                                    break
                            
                            if remaining_items:
                                debug_log(f"Master - Found {len(remaining_items)} items in queue after timeout!")
                                # Process them
                                for item in remaining_items:
                                    worker_id = item['worker_id']
                                    image_index = item['image_index']
                                    tensor = item['tensor']
                                    is_last = item.get('is_last', False)
                                    
                                    if worker_id not in worker_images:
                                        worker_images[worker_id] = {}
                                    worker_images[worker_id][image_index] = tensor
                                    
                                    collected_count += 1
                                    
                                    if is_last:
                                        workers_done.add(worker_id)
                                        debug_log(f"Master - Worker {worker_id} done (found in timeout drain)")
                        else:
                            log(f"Master - Queue {multi_job_id} no longer exists!")
                    break
            
            total_collected = sum(len(imgs) for imgs in worker_images.values())
            debug_log(f"Master - Collection complete. Received {total_collected} images from {len(workers_done)} workers")
            debug_log(f"Master - Workers done: {workers_done}, Enabled workers: {enabled_workers}")
            debug_log(f"Master - Worker images keys: {list(worker_images.keys())}")
            
            # Clean up job queue
            async with prompt_server.distributed_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_jobs:
                    del prompt_server.distributed_pending_jobs[multi_job_id]

            # Reorder images according to seed distribution pattern
            # Pattern: master img 1, master img 2, worker 1 img 1, worker 1 img 2, worker 2 img 1, worker 2 img 2, etc.
            ordered_tensors = []
            
            # Add master images first
            for i in range(master_batch_size):
                ordered_tensors.append(images_on_cpu[i:i+1])
            
            # Add worker images in order
            # The worker IDs in worker_images are already strings (e.g., "1", "2")
            # Just iterate through what we actually received
            for worker_id_str in sorted(worker_images.keys()):
                # Sort by image index for each worker
                for idx in sorted(worker_images[worker_id_str].keys()):
                    ordered_tensors.append(worker_images[worker_id_str][idx])
            
            # Ensure all tensors are on CPU and properly formatted before concatenation
            cpu_tensors = []
            for t in ordered_tensors:
                if t.is_cuda:
                    t = t.cpu()
                # Ensure tensor is contiguous in memory
                t = ensure_contiguous(t)
                cpu_tensors.append(t)
            
            try:
                combined = torch.cat(cpu_tensors, dim=0)
                # Ensure the combined tensor is contiguous and properly formatted
                combined = ensure_contiguous(combined)
                debug_log(f"Master - Job {multi_job_id} complete. Combined {combined.shape[0]} images total (master: {master_batch_size}, workers: {combined.shape[0] - master_batch_size})")
                return (combined,)
            except Exception as e:
                log(f"Master - Error combining images: {e}")
                debug_log(f"Master - Tensor shapes: {[t.shape for t in cpu_tensors]}")
                # Return just the master images as fallback
                return (images,)

# --- Distributor Node ---
class DistributedSeed:
    """
    Distributes seed values across multiple GPUs.
    On master: passes through the original seed.
    On workers: adds offset based on worker ID.
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seed": ("INT", {
                    "default": 0, 
                    "min": 0,
                    "max": 1125899906842624,
                    "forceInput": False  # Widget by default, can be converted to input
                }),
            },
            "hidden": {
                "is_worker": ("BOOLEAN", {"default": False}),
                "worker_id": ("STRING", {"default": ""}),
            },
        }
    
    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("seed",)
    FUNCTION = "distribute"
    CATEGORY = "utils"
    
    def distribute(self, seed, is_worker=False, worker_id=""):
        if not is_worker:
            # Master node: pass through original values
            debug_log(f"Distributor - Master: seed={seed}")
            return (seed,)
        else:
            # Worker node: apply offset based on worker index
            # Find worker index from enabled_worker_ids
            try:
                # Worker IDs are passed as "worker_0", "worker_1", etc.
                if worker_id.startswith("worker_"):
                    worker_index = int(worker_id.split("_")[1])
                else:
                    # Fallback: try to parse as direct index
                    worker_index = int(worker_id)
                
                offset = worker_index + 1
                new_seed = seed + offset
                debug_log(f"Distributor - Worker {worker_index}: seed={seed} → {new_seed}")
                return (new_seed,)
            except (ValueError, IndexError) as e:
                debug_log(f"Distributor - Error parsing worker_id '{worker_id}': {e}")
                # Fallback: return original seed
                return (seed,)

NODE_CLASS_MAPPINGS = { 
    "DistributedCollector": DistributedCollectorNode,
    "DistributedSeed": DistributedSeed
}
NODE_DISPLAY_NAME_MAPPINGS = { 
    "DistributedCollector": "Distributed Collector",
    "DistributedSeed": "Distributed Seed"
}
