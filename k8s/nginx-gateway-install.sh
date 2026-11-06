
#!/bin/bash
set -e

NAMESPACE="nginx-gateway"
VERSION="2.8.1"

echo "Installing Gateway API CRDs..."

kubectl kustomize \
  "https://github.com/nginx/nginx-gateway-fabric/config/crd/gateway-api/standard?ref=v${VERSION}" \
  | kubectl apply -f -

echo "Installing NGINX Gateway Fabric using Helm..."

helm upgrade --install ngf \
  oci://ghcr.io/nginx/charts/nginx-gateway-fabric \
  --version "${VERSION}" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --wait \
  --timeout 10m

echo "Waiting for NGINX Gateway Fabric..."

kubectl wait \
  --namespace "${NAMESPACE}" \
  --for=condition=Available \
  deployment/ngf-nginx-gateway-fabric \
  --timeout=300s

echo "Installation completed."

echo "========== HELM =========="
helm list -n "${NAMESPACE}"

echo "========== PODS =========="
kubectl get pods -n "${NAMESPACE}"

echo "========== SERVICES =========="
kubectl get svc -n "${NAMESPACE}"

echo "========== GATEWAY CLASS =========="
kubectl get gatewayclass
