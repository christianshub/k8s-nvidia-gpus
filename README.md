# Anisble, RKE2, Flux and NVIDIAs GPU Operator

This repo automatically sets up a single-node RKE2 Kubernetes cluster on RHEL8 using Ansible.
It then uses FluxCD to install NVIDIA’s GPU Operator, so the cluster can run a simple GPU workload.

## Requirements

Tested with the following setup:

- Ansible verison 2.10.8
- Python version 3.10.12
- Workstation OS: Ubuntu 22.04.5
- Target OS: RHEL8 (rhel-8.8-x86_64-dvd.iso) running in a VM
- A Red Hat Developer License or similar that gives access to update a RHEL8 machine (developers.redhat.com/register)

### Local secret management

[Pass](https://www.passwordstore.org/) and [direnv](https://direnv.net/) needs to be installed with a valid GitHub token flux can use. Pass and direnv is used to safely store our GitHub token (in a production setup you'd want to use a GitHub Service account that flux can leverage).
 

```bash
sudo apt install pass    # also available on Fedora/RHEL through dnf
gpg --full-generate-key  # exposes a gpg key
pass init <key>          # initiate pass database
pass insert GITHUB_TOKEN # Insert your github token here; rights to read repo content
cd k8s-nvidia-gpus/      # go to this repo
direnv allow             # enable directory env variables
env | grep GITHUB_TOKEN  # verification
```

## NVIDIA GPU preparations 

### VM or Bare-metal RHEL8 settings

> [!NOTE]  
> Run these commands **INSIDE** the RHEL8 OS.

1. Update your machine

    ```bash
    sudo subscription-nmanager register
    sudo dnf update -y
    ```

2. Verify a GPU is present:

    ```bash
    lspci -nnk | egrep -A3 -i 'nvidia|vga|3d|audio'
    # Example output: VGA compatible controller [0300]: NVIDIA Corporation GP104 [GeForce GTX 1070] [10de:1b81] (rev a1)
    ```

3. Set SELinux in permissive mode (it's important its not disabled). 

    Edit `/etc/selinux/config` so it looks like this:

    ```bash
    SELINUX=permissive
    SELINUXTYPE=targeted
    ```

    Verify:

    ```bash
    $ getenforce
    Permissive
    ```

    > You can also set this like with the command: `sudo setenforce 0`.

4. Verify nouveau doesn't load (this is the open source NVIDIA GPU driver) 

    ```bash
    echo -e "blacklist nouveau\noptions nouveau modeset=0" | sudo tee /etc/modprobe.d/blacklist-nouveau.conf
    sudo dracut -f
    ```

5. Consider disabling the firewall while testing:

    ```bash
    sudo systemctl disable --now firewalld
    ```

    Alternatively, allow the following:

    ```bash
    # API server
    sudo firewall-cmd --add-port=6443/tcp --permanent
    # Supervisor tunnel
    sudo firewall-cmd --add-port=9345/tcp --permanent
    # Kubelet (optional but often useful for metrics/logs)
    sudo firewall-cmd --add-port=10250/tcp --permanent
    # CNI overlay networking requires NAT
    sudo firewall-cmd --add-masquerade --permanent

    # Apply changes
    sudo firewall-cmd --reload
    ```

    Inspect the rules:

    ```bash
    sudo firewall-cmd --list-all
    sudo firewall-cmd --list-ports
    ```

    To clean up, remove them again if Kubernetes access is no longer needed:

    ```bash
    # Remove permanently
    sudo firewall-cmd --remove-port=6443/tcp --permanent
    sudo firewall-cmd --remove-port=9345/tcp --permanent
    sudo firewall-cmd --remove-port=10250/tcp --permanent

    # Apply changes
    sudo firewall-cmd --reload
    ```

6. Shutdown/reboot machine

    ```bash
    sudo poweroff
    # sudo reboot
    ```

### Proxmox settings (optional)

> [!NOTE]  
> Run these commands on the Proxmox (Debian) host, **not** inside the VM.

1. Enable IOMMU

    Intel:

    ```bash
    sudo sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="amd_iommu=on iommu=pt /' /etc/default/grub
    sudo update-grub
    ```

    AMD:

    ```bash
    sudo sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="amd_iommu=on iommu=pt /' /etc/default/grub
    sudo update-grub
    ```

2. Load vfio modules at boot

    ```bash
    sudo tee /etc/modules <<'EOF'
    vfio
    vfio_iommu_type1
    vfio_pci
    vfio_virqfd
    EOF
    ```

3. Bind your GPU to vfio-pci (replace IDs with yours)

    ```bash
    # Find your device IDs
    lspci -nn | egrep -i 'nvidia|vga|3d|audio'
    # Example (GTX 1070): GPU 10de:1b81, HDMI audio 10de:10f0

    echo 'options vfio-pci ids=10de:1b81,10de:10f0 disable_vga=1' | tee /etc/modprobe.d/vfio.conf
    update-initramfs -u
    reboot
    ```

4. Verify after reboot
   
    ```bash
    dmesg | egrep -i 'DMAR|IOMMU'   # look for "IOMMU enabled"
    lspci -k -s 01:00.0             # look for "kernel driver in use: vfio-pci"
    lspci -k -s 01:00.1             # look for "kernel driver in use: vfio-pci"
    ```

5. Attach GPU to VM
  
    Compute only (no GUI):

    ```bash
    qm set <VMID> -vga none
    qm set <VMID> -hostpci0 01:00.0
    qm set <VMID> -hostpci1 01:00.1
    qm set <VMID> -firewall 0       # consider only this while testing
    ```

    > If using multiple GPUs you'd also add these in a similar way and for example extend with `qm set <VMID> -hostpci2 06:00.0`

## RKE2 Setup

The following is to be executed on your RHEL8 OS - from your workstation (through Ansible)

### Steps

1. Deploy and install RKE2

    ```bash
    cd rke2-installation/
    ansible-playbook -i inventory.ini install-rke2.yaml -K
    ```

2. (Optional) You can follow along from within the RHEL8 OS:

    ```bash
    ssh <user>@<rke2-node>
    sudo journalctl -u rke2-server -n -f
    watch sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml -n kube-system get pods -o wide # in another console 
    ```

3. Fetch kubeconfig

    ```bash
    cd rke2-installation/
    ansible-playbook -i inventory.ini fetch-kubeconfig.yaml -K
    ```

4. Bootstrap flux:
 
    ```bash
    kubectl create namespace flux-system
    kubectl create secret generic flux-system \
    --namespace=flux-system \
    --from-literal=username=git \
    --from-literal=password="${GITHUB_TOKEN}"
    ```

5. Apply components (NVIDIAs GPU operator)

    ```bash
    kubectl apply -k cluster-config/cluster/flux-system/
    ```

### Verification one GPU did computing

Run the `vectoradd` verification test like shown in https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html#cuda-vectoradd:

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: cuda-vectoradd
  namespace: default
spec:
  backoffLimit: 0
  template:
    spec:
      runtimeClassName: nvidia
      restartPolicy: Never
      containers:
        - name: cuda-vectoradd
          image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0-ubi8
          resources:
            limits:
              nvidia.com/gpu: 2
EOF
```

Expected result from running `kubectl logs cuda-vectoradd-pc9t9`:

```bash                                                      
[Vector addition of 50000 elements]
Copy input data from the host memory to the CUDA device
CUDA kernel launch with 196 blocks of 256 threads
Copy output data from the CUDA device to the host memory
Test PASSED
Done
```

### Verification two GPUs did computing in parallel

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: two-pods-one-gpu
  namespace: default
spec:
  completions: 2
  parallelism: 2
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        app: vectoradd-verify
    spec:
      imagePullSecrets:
        - name: ngc-pull
      runtimeClassName: nvidia
      restartPolicy: Never
      containers:
        - name: vectoradd
          image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0-ubi8
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
          command: ["/bin/sh","-lc"]
          args:
            - |
              echo "===== POD: ${POD_NAME} ====="
              echo "===== ENV (NVIDIA/CUDA) ====="
              env | grep -E '^(NVIDIA|CUDA)_' || true
              echo "===== DEVICE NODES ====="
              ls -l /dev/nvidia* || true
              echo "===== NVIDIA-SMI (visible devices) ====="
              nvidia-smi -L
              echo "===== UUID TABLE ====="
              nvidia-smi --query-gpu=index,uuid,name --format=csv,noheader
              echo "===== RUN VECTORADD ====="
              /cuda-samples/vectorAdd
          resources:
            limits:
              nvidia.com/gpu: 1
EOF
```

Check results:

```bash
kubectl get pods -l job-name=two-pods-one-gpu
for p in $(kubectl get pods -l job-name=two-pods-one-gpu -o name); do
  echo "==== $p ===="; kubectl logs "$p"; echo; 
done
```

Expected result - something along the lines of this:

```sh
# pod 1
===== NVIDIA-SMI (visible devices) =====
GPU 0: NVIDIA GeForce GTX 1060 6GB (UUID: GPU-811027d8-8eb1-f08b-9cb2-24817c394031)
#
===== RUN VECTORADD =====
[Vector addition of 50000 elements]
Copy input data from the host memory to the CUDA device
CUDA kernel launch with 196 blocks of 256 threads
Copy output data from the CUDA device to the host memory
Test PASSED
Done

# pod 2
===== NVIDIA-SMI (visible devices) =====
GPU 0: NVIDIA GeForce GTX 1070 (UUID: GPU-9b6d7281-d5fc-2d68-1527-62d3ce658818)
#
===== RUN VECTORADD =====
[Vector addition of 50000 elements]
Copy input data from the host memory to the CUDA device
CUDA kernel launch with 196 blocks of 256 threads
Copy output data from the CUDA device to the host memory
Test PASSED
Done
```

### Verification one pod two GPUs computing

- TBD

### Uninstall RKE2

```bash
cd rke2-installation/
ansible-playbook -i inventory.ini uninstall-rke2.yaml -K
```

## Working remotely using tailscale

We are leveraging <https://tailscale.com/> to quickly access our homelab. First ensure you installed tailscale on your devices. 

1. Make a ssh-tunnel to your cluster:

  ```sh
  ssh -N -L 6443:127.0.0.1:6443 user@<cluster-tailscale-ip>
  ```

2. Make a ssh-tunnel to your proxmox instance:

  ```sh
  ssh -N -L 8006:127.0.0.1:8006 root@<proxmox-tailscale-ip>
  ```

3. Make an ssh-tunnel to your image generator

  ```sh
  ssh -N -L 30800:127.0.0.1:30800 user@100.73.2.47
  ```

4. Ensure your cluster config points at 127.0.0.1.

5. Test a service is up and running, e.g. sd15-api by executing the following command:

  ```sh
  curl -X POST \                          
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a cozy wooden cabin in a snowy forest at sunrise, warm window light, soft glow, high detail","steps":30}' \
  http://127.0.0.1:30800/generate \
  -o cabin.png
  ```

## SD15

sd15-api is a tiny REST server that generates images with Stable Diffusion 1.5.
It’s used here purely as a GPU sanity check: if a prompt returns an image and the pod sees an NVIDIA device, the RKE2 + Flux + GPU Operator setup is healthy.

Prompt example:

```sh
curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a cozy wooden cabin in a snowy forest at sunrise, warm window light, soft glow, high detail","steps":30}' \
  http://127.0.0.1:30800/generate \
  -o cabin.png
```

## Troubleshooting

### I can't get reach the Kubernetes cluster

Could be RHEL that blocks access by not allowing the needed ports k8s uses. 

1. Do a quick netcat inspection from your workstation:

    ```bash
    $ nc -vz <k8s-node-ip> 6443
    Unable to connect to the server: dial tcp <k8s-node-ip>:6443: connect: no route to host
    ```

    > For a quick test try temporarily disable the firewall: `sudo systemctl stop firewalld` and try use the command above again.

1. If the box has multiple interfaces, make sure firewalld allows from your client subnet:

    ```bash
    sudo firewall-cmd --add-rich-rule='rule family="ipv4" source address="192.168.0.0/24" port port="6443" protocol="tcp" accept' --permanent
    sudo firewall-cmd --reload
    ```
1. Access the kubernetes node and inspect firewall rules

    ```bash
    sudo firewall-cmd --list-all
    sudo firewall-cmd --list-ports
    ```

1. Apply firewall rules:

    ```bash
    # API server
    sudo firewall-cmd --add-port=6443/tcp --permanent
    # Supervisor tunnel
    sudo firewall-cmd --add-port=9345/tcp --permanent
    # Kubelet (optional but often useful for metrics/logs)
    sudo firewall-cmd --add-port=10250/tcp --permanent
    # CNI overlay networking requires NAT
    sudo firewall-cmd --add-masquerade --permanent

    # Apply changes
    sudo firewall-cmd --reload
    ``` 

1. To clean up, remove them again if Kubernetes access is no longer needed:

    ```bash
    # Remove permanently
    sudo firewall-cmd --remove-port=6443/tcp --permanent
    sudo firewall-cmd --remove-port=9345/tcp --permanent
    sudo firewall-cmd --remove-port=10250/tcp --permanent

    # Apply changes
    sudo firewall-cmd --reload
    ```
    
### Cilium has a cilium-operator pod in a pending state

We are right now deploying a one-node cluster, whereas cilium expects a HA cluster. To remove one operator pod, run the following:

```bash
kubectl -n kube-system scale deploy cilium-operator --replicas=1
kubectl -n kube-system get deploy cilium-operator
```

### Driver pod fails to build

  - Issue: Kernel headers didn’t match `uname -r` at build time.  
  - Fix: `sudo dnf -y update kernel kernel-headers kernel-devel && sudo reboot`.

### No NVIDIA RuntimeClass

  - Issue: Pods Pending ("runtime not found") leading to the operator couldn't update containerd.  
  - Debug: Double-check the RKE2 env vars:
  
    ```bash
    CONTAINERD_SOCKET=/run/k3s/containerd/containerd.sock
    CONTAINERD_CONFIG=/var/lib/rancher/rke2/agent/etc/containerd/config.toml
    ```

### Device Plugin shows 0 GPUs

  - Issue: After driver install, `/dev/nvidia*` must exist.  
  - Debug:  Check `ls -l /dev/nvidia*` and `dmesg | grep -i nvidia`.
