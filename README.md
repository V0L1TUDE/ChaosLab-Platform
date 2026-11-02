# ChaosLab & Online Boutique: Cloud-Native Microservices & Universal Resilience Platform

[![ChaosLab Test Suite](https://img.shields.io/badge/ChaosLab-Stage%2011%20Universal%20Generalization-blueviolet.svg)](#chaoslab-universal-resilience-platform)
[![Quality Gate](https://img.shields.io/badge/Quality%20Gate-APPROVED-success.svg)](#automated-quality-gate)
[![Continuous Integration](https://github.com/GoogleCloudPlatform/microservices-demo/workflows/Continuous%20Integration%20-%20Main/Release/badge.svg)](https://github.com/GoogleCloudPlatform/microservices-demo)

Welcome to the unified repository housing both **Google's Online Boutique** and **ChaosLab**, an enterprise-grade automated chaos testing, recovery verification, and distributed resilience platform.

---

## ChaosLab Universal Resilience Platform

**ChaosLab** is a decoupled, rule-based verification platform that proves microservice resilience in Kubernetes. Originally developed for Google's Online Boutique, ChaosLab has been universally generalized to test **arbitrary microservice applications** without rewriting code or creating service-specific Python verifiers.

### Architectural Overview

```
                                      +---------------------------------------------+
                                      |   Declarative YAML Experiment Configuration |
                                      +---------------------------------------------+
                                                             |
                                                             v
+------------------------------------------------------------------------------------------------------------------------+
|                                              CHAOSLAB UNIVERSAL ENGINE                                                 |
|                                                                                                                        |
|  +--------------------+     +---------------------+     +-----------------------+     +-----------------------------+  |
|  | K8s Infra Verifier |     | Generic HTTP Prober |     |  Generic gRPC Prober  |     |   Distributed Telemetry     |  |
|  | Pod / Node States  |     | Status / JSON Keys  |     | grpc.health.v1/Invoke |     |  OpenTelemetry / Jaeger     |  |
|  +--------------------+     +---------------------+     +-----------------------+     +-----------------------------+  |
|                                                            |                                                           |
|                                                            v                                                           |
|                                             +-------------------------------+                                          |
|                                             |   Universal 11-Rule Oracle    |                                          |
|                                             | PASS | FAIL | SKIP | NOT_APPL |                                          |
|                                             +-------------------------------+                                          |
|                                                            |                                                           |
|                                                            v                                                           |
|                                     +-----------------------------------------------+                                  |
|                                     | Multi-Format Reporter (JSON, Markdown, HTML)  |                                  |
|                                     +-----------------------------------------------+                                  |
+------------------------------------------------------------------------------------------------------------------------+
                                                             |
                 +-------------------------------------------+-------------------------------------------+
                 |                                           |                                           |
                 v                                           v                                           v
+---------------------------------+         +---------------------------------+         +---------------------------------+
|     Google Online Boutique      |         |   Project Management Platform   |         |    Learning Management System   |
|       (Namespace: default)      |         |  (Namespace: project-platform)  |         |  (Namespace: learning-platform) |
|  - frontend       - checkout    |         |  - pulse-frontend (React UI)    |         |  - lms-frontend (React UI)      |
|  - paymentservice - cartservice |         |  - user-service    (Port 8001)  |         |  - student-service  (Port 8011) |
|  - 11 microservices (gRPC)      |         |  - project-service (Port 8002)  |         |  - course-service   (Port 8012) |
|                                 |         |  - task-service    (Port 8003)  |         |  - enrollment-svc   (Port 8013) |
+---------------------------------+         +---------------------------------+         +---------------------------------+
```

### Supported Target Platforms

ChaosLab simultaneously validates 3 independent application platforms in the cluster:

1. **Google Online Boutique** (`default` namespace):
   - 11 heterogeneous microservices (Go, C#, Node.js, Python, Java).
   - High-throughput e-commerce flow with gRPC interfaces and mock payment processing.
2. **Pulse Project Management Platform** (`project-platform` namespace):
   - Production SaaS React frontend with live task kanban, project analytics, and team directory.
   - Three FastAPI microservices (`user-service`, `project-service`, `task-service`).
   - Inter-service REST communication with OpenTelemetry distributed trace context propagation.
3. **EduStream Learning Management System** (`learning-platform` namespace):
   - Interactive LMS React dashboard featuring course catalog, syllabus explorer, and enrollment tracking.
   - Three FastAPI microservices (`student-service`, `course-service`, `enrollment-service`).
   - Cross-service enrollment transactions with distributed telemetry exported to Jaeger.

---

### Quick Start & Verification CLI

#### 1. List All Discovered Experiments across Applications
```powershell
python .\chaoslab\runner.py list
```

#### 2. Execute a Single Universal Experiment
```powershell
# Test Project Management Platform task service
python .\chaoslab\runner.py run project-task-pod-kill

# Test Learning Management System course service
python .\chaoslab\runner.py run lms-course-pod-kill

# Test Network Latency on Project Platform
python .\chaoslab\runner.py run project-task-network-delay

# Regression test on Online Boutique (100% backward compatible)
python .\chaoslab\runner.py run checkout-service-pod-kill
```

#### 3. Run the Full Multi-Application Regression Suite
```powershell
python .\chaoslab\runner.py run-all
```

#### 4. Audit CI/CD Quality Gate
```powershell
python .\chaoslab\gate.py check
```

#### 5. Launch the Interactive Web Dashboard
```powershell
python .\chaoslab\runner.py dashboard --port 8080
```
Open [http://localhost:8080](http://localhost:8080) to access the multi-application topology grid, live execution stepper, and Oracle scorecard.

---

### Declarative Universal YAML Schema

ChaosLab experiments are fully decoupled from code:

```yaml
name: project-task-pod-kill

target:
  namespace: project-platform
  label:
    app: task-service

fault:
  type: pod-kill
  mode: one

recovery:
  timeout_seconds: 60
  expected_ready_replicas: 1

application:
  enabled: true
  health_probe:
    type: http
    port: 8003
    path: /health
    method: GET
    expected_status: 200
  functional_check:
    enabled: true
    type: http
    port: 8003
    path: /tasks/test
    method: GET
    expected_status: 200
    expected_key: task_id
  logs:
    verify_logs: true
    expected_log_pattern: "Uvicorn running"

telemetry:
  enabled: true
  service_name: "task-service"
  max_allowed_p95_ms: 1000
```

---

## Original Online Boutique Documentation

Google uses this application to demonstrate how developers can modernize enterprise applications using Google Cloud products, including: [Google Kubernetes Engine (GKE)](https://cloud.google.com/kubernetes-engine), [Cloud Service Mesh (CSM)](https://cloud.google.com/service-mesh), [gRPC](https://grpc.io/), [Cloud Operations](https://cloud.google.com/products/operations), [Spanner](https://cloud.google.com/spanner), [Memorystore](https://cloud.google.com/memorystore), [AlloyDB](https://cloud.google.com/alloydb), and [Gemini](https://ai.google.dev/). This application works on any Kubernetes cluster.

If you’re using this demo, please **★Star** this repository to show your interest!

**Note to Googlers:** Please fill out the form at [go/microservices-demo](http://go/microservices-demo).

## Architecture

**Online Boutique** is composed of 11 microservices written in different
languages that talk to each other over gRPC.

[![Architecture of
microservices](/docs/img/architecture-diagram.png)](/docs/img/architecture-diagram.png)

Find **Protocol Buffers Descriptions** at the [`./protos` directory](/protos).

| Service                                              | Language      | Description                                                                                                                       |
| ---------------------------------------------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| [frontend](/src/frontend)                           | Go            | Exposes an HTTP server to serve the website. Does not require signup/login and generates session IDs for all users automatically. |
| [cartservice](/src/cartservice)                     | C#            | Stores the items in the user's shopping cart in Redis and retrieves it.                                                           |
| [productcatalogservice](/src/productcatalogservice) | Go            | Provides the list of products from a JSON file and ability to search products and get individual products.                        |
| [currencyservice](/src/currencyservice)             | Node.js       | Converts one money amount to another currency. Uses real values fetched from European Central Bank. It's the highest QPS service. |
| [paymentservice](/src/paymentservice)               | Node.js       | Charges the given credit card info (mock) with the given amount and returns a transaction ID.                                     |
| [shippingservice](/src/shippingservice)             | Go            | Gives shipping cost estimates based on the shopping cart. Ships items to the given address (mock)                                 |
| [emailservice](/src/emailservice)                   | Python        | Sends users an order confirmation email (mock).                                                                                   |
| [checkoutservice](/src/checkoutservice)             | Go            | Retrieves user cart, prepares order and orchestrates the payment, shipping and the email notification.                            |
| [recommendationservice](/src/recommendationservice) | Python        | Recommends other products based on what's given in the cart.                                                                      |
| [adservice](/src/adservice)                         | Java          | Provides text ads based on given context words.                                                                                   |
| [loadgenerator](/src/loadgenerator)                 | Python/Locust | Continuously sends requests imitating realistic user shopping flows to the frontend.                                              |

## Screenshots

| Home Page                                                                                                         | Checkout Screen                                                                                                    |
| ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| [![Screenshot of store homepage](/docs/img/online-boutique-frontend-1.png)](/docs/img/online-boutique-frontend-1.png) | [![Screenshot of checkout screen](/docs/img/online-boutique-frontend-2.png)](/docs/img/online-boutique-frontend-2.png) |

## Quickstart (GKE)

1. Ensure you have the following requirements:
   - [Google Cloud project](https://cloud.google.com/resource-manager/docs/creating-managing-projects#creating_a_project).
   - Shell environment with `gcloud`, `git`, and `kubectl`.

2. Clone the latest major version.

   ```sh
   git clone --depth 1 --branch v0 https://github.com/GoogleCloudPlatform/microservices-demo.git
   cd microservices-demo/
   ```

   The `--depth 1` argument skips downloading git history.

3. Set the Google Cloud project and region and ensure the Google Kubernetes Engine API is enabled.

   ```sh
   export PROJECT_ID=<PROJECT_ID>
   export REGION=us-central1
   gcloud services enable container.googleapis.com \
     --project=${PROJECT_ID}
   ```

   Substitute `<PROJECT_ID>` with the ID of your Google Cloud project.

4. Create a GKE cluster and get the credentials for it.

   ```sh
   gcloud container clusters create-auto online-boutique \
     --project=${PROJECT_ID} --region=${REGION}
   ```

   Creating the cluster may take a few minutes.

5. Deploy Online Boutique to the cluster.

   ```sh
   kubectl apply -f ./release/kubernetes-manifests.yaml
   ```

6. Wait for the pods to be ready.

   ```sh
   kubectl get pods
   ```

   After a few minutes, you should see the Pods in a `Running` state:

   ```
   NAME                                     READY   STATUS    RESTARTS   AGE
   adservice-76bdd69666-ckc5j               1/1     Running   0          2m58s
   cartservice-66d497c6b7-dp5jr             1/1     Running   0          2m59s
   checkoutservice-666c784bd6-4jd22         1/1     Running   0          3m1s
   currencyservice-5d5d496984-4jmd7         1/1     Running   0          2m59s
   emailservice-667457d9d6-75jcq            1/1     Running   0          3m2s
   frontend-6b8d69b9fb-wjqdg                1/1     Running   0          3m1s
   loadgenerator-665b5cd444-gwqdq           1/1     Running   0          3m
   paymentservice-68596d6dd6-bf6bv          1/1     Running   0          3m
   productcatalogservice-557d474574-888kr   1/1     Running   0          3m
   recommendationservice-69c56b74d4-7z8r5   1/1     Running   0          3m1s
   redis-cart-5f59546cdd-5jnqf              1/1     Running   0          2m58s
   shippingservice-6ccc89f8fd-v686r         1/1     Running   0          2m58s
   ```

7. Access the web frontend in a browser using the frontend's external IP.

   ```sh
   kubectl get service frontend-external | awk '{print $4}'
   ```

   Visit `http://EXTERNAL_IP` in a web browser to access your instance of Online Boutique.

8. Congrats! You've deployed the default Online Boutique. To deploy a different variation of Online Boutique (e.g., with Google Cloud Operations tracing, Istio, etc.), see [Deploy Online Boutique variations with Kustomize](#deploy-online-boutique-variations-with-kustomize).

9. Once you are done with it, delete the GKE cluster.

   ```sh
   gcloud container clusters delete online-boutique \
     --project=${PROJECT_ID} --region=${REGION}
   ```

   Deleting the cluster may take a few minutes.

## Additional deployment options

- **Terraform**: [See these instructions](/terraform) to learn how to deploy Online Boutique using [Terraform](https://www.terraform.io/intro).
- **Istio / Cloud Service Mesh**: [See these instructions](/kustomize/components/service-mesh-istio/README.md) to deploy Online Boutique alongside an Istio-backed service mesh.
- **Non-GKE clusters (Minikube, Kind, etc)**: See the [Development guide](/docs/development-guide.md) to learn how you can deploy Online Boutique on non-GKE clusters.
- **AI assistant using Gemini**: [See these instructions](/kustomize/components/shopping-assistant/README.md) to deploy a Gemini-powered AI assistant that suggests products to purchase based on an image.
- **And more**: The [`/kustomize` directory](/kustomize) contains instructions for customizing the deployment of Online Boutique with other variations.

## Documentation

- [Development](/docs/development-guide.md) to learn how to run and develop this app locally.

## Demos featuring Online Boutique

- [Security hardening of the OnlineBoutique sample apps with the Docker Hardened Images (DHI)](https://medium.com/google-cloud/security-hardening-of-the-onlineboutique-sample-apps-with-docker-hardened-images-dhi-ca1fad348343)
- [alpine, distroless or scratch?](https://medium.com/google-cloud/alpine-distroless-or-scratch-caac35250e0b)
- [Platform Engineering in action: Deploy the Online Boutique sample apps with Score and Humanitec](https://medium.com/p/d99101001e69)
- [The new Kubernetes Gateway API with Istio and Anthos Service Mesh (ASM)](https://medium.com/p/9d64c7009cd)
- [Use Azure Redis Cache with the Online Boutique sample on AKS](https://medium.com/p/981bd98b53f8)
- [Sail Sharp, 8 tips to optimize and secure your .NET containers for Kubernetes](https://medium.com/p/c68ba253844a)
- [Deploy multi-region application with Anthos and Google cloud Spanner](https://medium.com/google-cloud/a2ea3493ed0)
- [Use Google Cloud Memorystore (Redis) with the Online Boutique sample on GKE](https://medium.com/p/82f7879a900d)
- [Use Helm to simplify the deployment of Online Boutique, with a Service Mesh, GitOps, and more!](https://medium.com/p/246119e46d53)
- [How to reduce microservices complexity with Apigee and Anthos Service Mesh](https://cloud.google.com/blog/products/application-modernization/api-management-and-service-mesh-go-together)
- [gRPC health probes with Kubernetes 1.24+](https://medium.com/p/b5bd26253a4c)
- [Use Google Cloud Spanner with the Online Boutique sample](https://medium.com/p/f7248e077339)
- [Seamlessly encrypt traffic from any apps in your Mesh to Memorystore (redis)](https://medium.com/google-cloud/64b71969318d)
- [Strengthen your app's security with Cloud Service Mesh and Anthos Config Management](https://cloud.google.com/service-mesh/docs/strengthen-app-security)
- [From edge to mesh: Exposing service mesh applications through GKE Ingress](https://cloud.google.com/architecture/exposing-service-mesh-apps-through-gke-ingress)
- [Take the first step toward SRE with Cloud Operations Sandbox](https://cloud.google.com/blog/products/operations/on-the-road-to-sre-with-cloud-operations-sandbox)
- [Deploying the Online Boutique sample application on Cloud Service Mesh](https://cloud.google.com/service-mesh/docs/onlineboutique-install-kpt)
- [Anthos Service Mesh Workshop: Lab Guide](https://codelabs.developers.google.com/codelabs/anthos-service-mesh-workshop)
- [KubeCon EU 2019 - Reinventing Networking: A Deep Dive into Istio's Multicluster Gateways - Steve Dake, Independent](https://youtu.be/-t2BfT59zJA?t=982)
- Google Cloud Next'18 SF
  - [Day 1 Keynote](https://youtu.be/vJ9OaAqfxo4?t=2416) showing GKE On-Prem
  - [Day 3 Keynote](https://youtu.be/JQPOPV_VH5w?t=815) showing Stackdriver
    APM (Tracing, Code Search, Profiler, Google Cloud Build)
  - [Introduction to Service Management with Istio](https://www.youtube.com/watch?v=wCJrdKdD6UM&feature=youtu.be&t=586)
- [Google Cloud Next'18 London – Keynote](https://youtu.be/nIq2pkNcfEI?t=3071)
  showing Stackdriver Incident Response Management
- [Microservices demo showcasing Go Micro](https://github.com/go-micro/demo)
