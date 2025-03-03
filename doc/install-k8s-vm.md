# Install single-node Kubernetes cluster in a virtual machine

[Kubernetes](https://kubernetes.io), also known as K8s, is an open source system for managing containerized applications across multiple hosts. It provides basic mechanisms for the deployment, maintenance, and scaling of applications. Kubernetes provides an abstraction layer to actually orchestrating experimentation resources (all the way from containers, CPU, memory up to programmable NICs, GPU, even external resources).

This document describe how you can create a single-node Kubernetes cluster on your computer using a virtual machine! You will need the following requirements:

- virtual machine running Debian stable (example: Debian 12). Basic installation without any graphical interface is fine (only select the taskel "standard system utilities"). 

- the VM should have 4GB of RAM

- the VM should have 4 vCPUs

- the VM should have 20GB of disk space

- the VM should have Internet access

## Step-by-Step setup

Once you installed the virtual machine as described above, proceed with the steps below.

1. Edit the Grub config file (`/etc/default/grub`) to allow old containers to run (containers based on very old glibc libs):

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet vsyscall=emulate"
```

After editting the file, you will need to reconfigure grub and reload:
```
update-grub
reboot
```

2. Kubernetes requires some sysctl configs and Kernel modules to be loaded. For sysctl config:

```
cat > /etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-ip6tables = 1
net.bridge.bridge-nf-call-iptables = 1
net.ipv4.ip_forward = 1
EOF
sysctl --system
```

For Kernel modules:
```
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter
```

3. The next step is to install Kubernetes utilities: kubelet, kubeadm and kubectl. Run the following commands:

```
apt-get install -y apt-transport-https ca-certificates curl gpg
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /' | tee /etc/apt/sources.list.d/kubernetes.list > /dev/null
apt-get update
apt-get install -y kubelet kubeadm kubectl
```

4. Next we will need to install a container management software:

```

curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt install -y containerd.io
```

5. Some configurations are needed for containerd:

```
mkdir -p /etc/containerd
containerd config default | tee /etc/containerd/config.toml > /dev/null
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml

systemctl restart containerd
systemctl enable containerd
systemctl status containerd
```

6. Create crictl.yaml config file in /etc:

```
cat > /etc/crictl.yaml <<EOF
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 2
debug: false # <- if you don't want to see debug info you can set this to false
pull-image-on-create: false
EOF

sudo crictl ps
```

7. Finally, you will be able to now start your Kubernetes cluster setup:

```
kubeadm init --pod-network-cidr 10.96.0.0/16 --service-cidr 10.97.0.0/16 --service-dns-domain myk8s.local
```

This command can take a few minutes, because all container images will be downloaded and then the cluster will be configured.

8. Once the command above finishes, you will see a message saying that the Cluster was setup correctly and then you need to create the Kube config to access the resources:

```
mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config

kubectl get nodes
```

Note that you can see the control-plane node here, but the status is not ready. We need to apply the CNI plugin to make this control-plane ready.

9. To setup CNI, follow the steps below:

```
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.2/manifests/tigera-operator.yaml
curl -LO https://raw.githubusercontent.com/projectcalico/calico/v3.29.2/manifests/custom-resources.yaml
sed -i "s/blockSize: 26/blockSize: 24/g" custom-resources.yaml
sed -i "s/cidr: 10.252.0.0/cidr: 10.96.0.0/g" custom-resources.yaml
kubectl create -f custom-resources.yaml
```

Once again, container images will be downloaded to setup CNI. After a few minutes you can check the nodes again to make sure the status is now Ready.

10. Making the control node to also be able to schedule pods:

```
kubectl taint node --all node-role.kubernetes.io/control-plane:NoSchedule-
```

11. Create a new namespace and add a namespace admin user to it. We start by first creating some template files that will be used to facilitate the user/namespace setup:

```
cat > rbac.yaml <<EOF
# custom ClusterRole to allow users to see nodes on the cluster (see more on create-cluster-nodes-view.yaml)
kind: ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: XXXNAMESPACEXXX_XXXUSERNAMEXXX_cluster-nodes-view
roleRef:
  kind: ClusterRole
  name: cluster-nodes-view
  apiGroup: rbac.authorization.k8s.io
subjects:
- kind: User
  name: system:serviceaccount:XXXNAMESPACEXXX:XXXUSERNAMEXXX
  apiGroup: rbac.authorization.k8s.io
---
# standard ClusterRole called edit (kubectl get clusterrole edit -o yaml) which will allow the user to manage its namesapce
kind: RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: XXXNAMESPACEXXX_XXXUSERNAMEXXX_ns-user-edit
  namespace: XXXNAMESPACEXXX
roleRef:
  kind: ClusterRole
  name: edit
  apiGroup: rbac.authorization.k8s.io
subjects:
- kind: User
  name: system:serviceaccount:XXXNAMESPACEXXX:XXXUSERNAMEXXX
  apiGroup: rbac.authorization.k8s.io
EOF

cat > create-cluster-nodes-view.yaml <<EOF
# kubectl apply -f create-cluster-nodes-view.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-nodes-view
rules:
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["apiextensions.k8s.io"]
  resources: ["customresourcedefinitions"]
  verbs: ["get", "list", "watch"]
EOF

cat > secret.yaml <<EOF
#=== secret.yaml ===
# kubectl -n #namespace  apply -f secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: XXXUSERNAMEXXX-secret
  annotations:
    kubernetes.io/service-account.name: XXXUSERNAMEXXX
type: kubernetes.io/service-account-token
EOF

cat > kube-config.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: XXX_DOMAIN_XXX
    cluster:
      server: XXX_APIURL_XXX
      certificate-authority-data: XXX_CERT_XXX
users:
  - name: XXXUSERNAMEXXX
    user:
      token: XXXTOKENXXX
contexts:
  - name: XXXNAMESPACEXXX_XXXUSERNAMEXXX_sa@XXX_DOMAIN_XXX
    context:
      user: XXXUSERNAMEXXX
      cluster: XXX_DOMAIN_XXX
      namespace: XXXNAMESPACEXXX
current-context: XXXNAMESPACEXXX_XXXUSERNAMEXXX_sa@XXX_DOMAIN_XXX
preferences: {}
EOF
```

Then we can finally setup the namespace and its admin user:

```
DOMAIN=myk8s.local
CERT=$(kubectl config view --flatten --output jsonpath="{.clusters[*].cluster.certificate-authority-data}")
API_SERVER=$(kubectl config view --output jsonpath="{.clusters[*].cluster.server}")
USERNAME=ns-admin
NAMESPACE=hackinsdn

kubectl create namespace $NAMESPACE
kubectl -n $NAMESPACE create sa $USERNAME
sed -e "s/XXXUSERNAMEXXX/$USERNAME/g" secret.yaml | kubectl -n $NAMESPACE apply -f -
TOKEN=$(kubectl -n $NAMESPACE get secret/$USERNAME-secret -o jsonpath='{.data.token}' | base64 --decode)
sed -e "s/XXXUSERNAMEXXX/$USERNAME/g; s/XXXNAMESPACEXXX/$NAMESPACE/g" rbac.yaml | kubectl -n $NAMESPACE apply -f -
sed -e "s/XXXUSERNAMEXXX/$USERNAME/g; s/XXXNAMESPACEXXX/$NAMESPACE/g; s/XXXTOKENXXX/$TOKEN/g; s/XXX_DOMAIN_XXX/$DOMAIN/g; s#XXX_APIURL_XXX#$API_SERVER#g; s/XXX_CERT_XXX/$CERT/g" kube-config.yaml > kube-config-$USERNAME@$NAMESPACE.yaml
```

After running the commands above, you can copy the resulting file `kube-config-ns-admin@hackinsdn.yaml` to the Dasboard setup (or to your machine to be used with kubectl)

## Testing

You can run a few tests to make sure the Kubernetes cluster was setup correctly. We will be assuming those commands below will be executed in another computer with `kubectl` installed (see more information in https://kubernetes.io/docs/tasks/tools/#kubectl) and that you copied the kube config file from previous section (`kube-config-ns-admin@hackinsdn.yaml`) into your `~/.kube/config` file system.

1. Get the list of nodes:

```
kubectl get nodes
```

You should see something similar to:

```
hackinsdn@hackinsdn-k8s-deb12:~$ kubectl get nodes
NAME                  STATUS   ROLES           AGE   VERSION
hackinsdn-k8s-deb12   Ready    control-plane   19h   v1.32.2
```

2. Run a Hello World application with

```
kubectl apply -f https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/manifest.yml
```

You should see something like this:
```
hackinsdn@hackinsdn-k8s-deb12:~$ kubectl apply -f https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/manifest.yml
deployment.apps/helloworld-hackinsdn created
service/helloworld-hackinsdn created

hackinsdn@hackinsdn-k8s-deb12:~$ kubectl get pods
NAME                                    READY   STATUS    RESTARTS   AGE
helloworld-hackinsdn-84c9f6444c-8q7hz   1/1     Running   0          83s

hackinsdn@hackinsdn-k8s-deb12:~$ kubectl get deployment
NAME                   READY   UP-TO-DATE   AVAILABLE   AGE
helloworld-hackinsdn   1/1     1            1           88s

hackinsdn@hackinsdn-k8s-deb12:~$ kubectl get service
NAME                   TYPE       CLUSTER-IP    EXTERNAL-IP   PORT(S)        AGE
helloworld-hackinsdn   NodePort   10.97.75.41   <none>        80:31155/TCP   91s
```

Finally, you can remove the resources (we will create them again when setting up the Dashboard):
```
kubectl delete -f https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/manifest.yml
```
