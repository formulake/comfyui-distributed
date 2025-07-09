# ComfyUI-Distributed

![Clipboard Image (3)](https://github.com/user-attachments/assets/19fdd1be-8f6e-4df5-bcd9-538ef566fa82)

> **Supercharge your ComfyUI workflows with multi-GPU processing**

A powerful extension for ComfyUI that enables parallel and distributed processing across multiple GPUs and machines. Speed up your image generation and upscaling workflows by leveraging all available GPU resources in your network.

📺 [Watch the Tutorial](https://www.youtube.com/watch?v=p6eE3IlAbOs)

---

## Key Features

#### Parallel Workflow Processing
- **Parallel Generation** - Run the same workflow on multiple GPUs simultaneously with different seeds
- **Automatic Load Balancing** - Distribute workflow execution across available workers
- **Batch Acceleration** - Generate multiple variations faster by using all your GPUs

#### Distributed Upscaling
- **True Distributed Processing** - Split large upscaling tasks into tiles processed across multiple GPUs
- **Tile-based Upscaling** - Intelligent work distribution for Ultimate SD Upscale

#### Management & Monitoring
- **Automatic Worker Management** - Launch and monitor workers from the UI
- **Network Support** - Use GPUs across different machines on your network
- **Real-time Monitoring** - Track worker status and performance from the UI
- **Easy Configuration** - JSON-based configuration with UI controls
- **Memory Management** - Built-in VRAM clearing

---

## Requirements

- ComfyUI installation
- Multiple GPUs
- That's it

---

## Installation

1. **Clone this repository** into your ComfyUI custom nodes directory:
   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/yourusername/ComfyUI-Distributed.git
   ```

2. **Restart ComfyUI** - If you'll be using remote workers, add `--enable-cors-header` to your launch arguments

---

## Quick Start

### Adding Local Workers
![Distributed GPU Panel](https://github.com/user-attachments/assets/9c1d6d0e-3fd1-43e3-97c4-7c6bf2952b19)
> Local Workers: Additional ComfyUI instances running on the same computer (with multi-GPUs) as your main ComfyUI installation.

1. **Open** the Distributed GPU panel.
2. **Click** "Add Worker" in the UI.
3. **Configure** your local worker:
   - **Name**: A descriptive name for the worker (e.g., "My Gaming PC GPU 0")
   - **Port**: A unique port number for this worker (e.g., 8189, 8190...).
   - **CUDA Device**: The GPU index from `nvidia-smi` (e.g., 0, 1).
   - **Extra Args**: Optional ComfyUI arguments for this specific worker.
4. **Save** and optionally launch the local worker.

### Adding Remote Workers

> Remote Workers: ComfyUI instances running on completely different computers on your network. These allow you to harness GPU power from other machines. Remote workers must be manually started on their respective computers and are connected via IP address.

1. **On the Remote Worker Machine:**
   - **Launch** ComfyUI with the `--listen --enable-cors-header` arguments. ⚠️ **Required!**
   - **Add** workers in the UI panel if the remote machine has more than one GPU.
      - Make sure that they also have `--listen` set in `Extra Args`.
      - Then launch them.
   - **Open** the configured worker port(s) (e.g., 8189, 8190) in the remote worker's firewall.
  
2. **On the Main Machine:**
   - **Open** the Distributed GPU panel (sidebar on the left).
   - **Click** "Add Worker."
   - **Enable** "Remote Worker" checkbox.
   - **Configure** your remote worker:
     - **Name**: A descriptive name for the worker (e.g., "Server Rack GPU 0")
     - **Host**: The remote worker's IP address.
     - **Port**: The port number used when launching ComfyUI on the remote worker (e.g., 8189).
   - **Save** the remote worker configuration.

### Configuration Tips

| Setting | Description | Example |
|---------|-------------|---------|
| **CUDA Devices** | Use `nvidia-smi` to see GPU indices | 0, 1, 2... |
| **Ports** | Each worker needs a unique port | 8189, 8190... |
| **Extra Args** | Additional ComfyUI arguments | See below |

**Common Extra Args:**
- `--listen` - **Required** for remote workers
- `--enable-cors-header` - **Required** if using remoter workers
- `--lowvram` - For GPUs with less memory
- `--highvram` - For high-end GPUs
- `--reserve-vram 2` - Reserves 2GB of VRAM. Recommended for your primary/display GPU

---

## Nodes

### Distributed Collector
> Collects and combines results from distributed processing

**Usage**: Place after any image generation node to enable distributed processing. Works automatically when workers are enabled.

### Distributed Seed
> Ensures unique seeds across distributed workers for varied generations

**Usage**: Connect to any seed input. Each worker automatically receives an offset seed to ensure randomisation. Alternatively, you can connect a seed node directly to this node's seed input, which will automatically handle seed offsetting across all workers.

### Ultimate SD Upscale Distributed
> Distributed version of Ultimate SD Upscale that processes tiles across multiple GPUs, making upscaling much faster

**Usage**:
1. Upscale your image with a regular upscale model (ESRGAN, etc.)
2. Feed the upscaled image into this node
3. Configure tile settings
4. Enable workers for fast processing

---

## UI Features

### Distributed GPU Panel

The control centre for your distributed setup:

| Feature | Description |
|---------|-------------|
| **Worker Status** | Real-time status indicators |
| **Launch/Stop** | Control individual local workers |
| **Clear Memory** | Free VRAM on all workers |
| **Interrupt** | Stop current processing |
| **Worker Logs** | View real-time logs |

---

## Workflow Examples

### Basic Distributed Generation

![Clipboard Image (1)](https://github.com/user-attachments/assets/e8e46d97-d698-4c18-b4e5-1e1a2f4f7da3)

1. Create your normal ComfyUI workflow
2. Add **Distributed Seed** → connect to sampler's seed
3. Add **Distributed Collector** → after VAE Decode
4. Enable workers in the UI
5. Run the workflow!

### Distributed Upscaling

![Clipboard Image (2)](https://github.com/user-attachments/assets/ec2548d0-1fc7-4705-801f-3270d720cfce)

1. Load your image
2. Upscale with ESRGAN or similar
3. Connect to **Ultimate SD Upscale Distributed**
4. Configure tile settings
5. Enable workers for faster processing

---

## Troubleshooting

### Common Issues

<details>
<summary><b>Workers won't start</b></summary>

- Check ports are not in use: `netstat -an | grep 8189`
- Verify CUDA device exists: `nvidia-smi`
- Check ComfyUI path in worker logs
</details>

<details>
<summary><b>"Worker not managed by UI" message</b></summary>

- Worker was started outside the UI
- Stop the worker manually and use the UI to relaunch
</details>

<details>
<summary><b>Images not combining properly</b></summary>

- Ensure all remote workers have the same models available
- Check that custom nodes are installed on all remote workers
</details>

<details>
<summary><b>Network connection issues</b></summary>

- Check firewall settings for required ports
- Verify master IP is accessible: `ping 192.168.1.100`
- Ensure same ComfyUI version on all machines
- Ensure ComfyUI-Distributed is installed on remote workers
</details>

## UI Panel Settings

The Distributed GPU panel includes several configuration options in the Settings section:

### Debug Mode
- **Description**: Enable detailed logging for troubleshooting and monitoring distributed operations
- **Default**: Disabled
- **Usage**: When enabled, detailed debug information is logged to the browser console, including worker status updates, job distribution details, and network communication logs

### Auto-launch Workers on Startup
- **Description**: Automatically launch enabled local workers when ComfyUI starts
- **Default**: Disabled  
- **Usage**: When enabled, any local workers that are marked as "enabled" will be automatically launched in the background when the ComfyUI server starts, eliminating the need to launch each worker manually

### Stop Workers on Master Exit
- **Description**: Automatically stop all managed local workers when the master ComfyUI instance shuts down
- **Default**: Enabled
- **Usage**: When enabled, ensures clean shutdown by stopping all UI-managed worker processes when the main ComfyUI server exits, preventing orphaned background processes

---

## Development

This project is under active development. Contributions are welcome!

### Planned Features

- [ ] Remote worker control via SSH
- [ ] View remote worker logs in UI
- [ ] Improve worker timeout logic
- [ ] Support for Runpod workers
