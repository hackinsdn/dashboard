# Dashboard HackInSDN Install guide

This document describe the steps for installing Dashboard HackInSDN and integrate it with a Kubernetes cluster. The document is structured in the following way:

[Pre-requirements](#prereq)  
[Running with Docker](#run-with-docker)
[Installing step-by-step](#install)
[Running a hello world](#helloworld)  

<a name="prereq"/>
## Pre-requirements

Two main pre requirements are necessary to run Dashboard with its main funcionality: 1) **Authentication** credentials with a Oauth2 provider; 2) **Kubernetes** credentials for a cluster that will provide the actual experimentation resources.

### Authentication

Dashboard HackInSDN supports *local authentication* and *federated authentication*. Local authentication is enabled by default when installing Dashboard HackInSDN and can also be used together with federated authentication. Federated authentication rely on a Oauth2 provider to actually interact with a Identify Provider and authenticate users. There are many Oauth2 authentication providers available for use, including: [CILogon](https://www.cilogon.org/oidc), [ORCID](https://info.orcid.org/documentation/api-tutorials/api-tutorial-get-and-authenticated-orcid-id/), [Auth0](https://auth0.com/), [Google Identify](https://developers.google.com/identity/protocols/oauth2), [Facebook/Meta Login](https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow), and the list keeps growing.

CILogon and ORCID are two very interesting Oauth2 providers because they have integration with academic identity providers, enabling users to login with their organization accounts (students, professors, researches, faculty, etc). Choose one of them (or any other Oauth provider of your interest) and create the Application Credentials to integrate with Dashboard HackInSDN. Below are two links with documentation for CILogon and ORCID:

- [CILogon OpenID Connect (OIDC) Client Registration](https://cilogon.org/oauth2/register)

- [ORCID Registering a Public API client](https://info.orcid.org/documentation/integration-guide/registering-a-public-api-client/)

Once you have the client registration information, you will need the following information to configure Dashboard HackInSDN (those information dont actually need to be exported like showed below: you can configure them into the `apps/config.py` file. We show below just for ilustrate the information needed):

```
# example for CILogon
export OAUTH_CLIENT_ID=cilogon:/client_id/xxxxyyyyzzzz
export OAUTH_CLIENT_SECRET=xxxyyzzwww-aaabbcc112233
export OAUTH_DOMAIN=cilogon.org

# example for ORCID
export OAUTH_CLIENT_ID=APP-xxxyyyzzz
export OAUTH_CLIENT_SECRET=xxx-yyy-zzz-www-aaaa
export OAUTH_DOMAIN=orcid.org
```

### Kubernetes

Dashboard HackInSDN is a web platform for running virtual laboratories based on a cloud service model. Thus, in order to actually run your experiments, you will need an execution environment. More specifically, you will need access to a Kubernetes cluster. [Kubernetes](https://kubernetes.io), also known as K8s, is an open source system for managing containerized applications across multiple hosts. It provides basic mechanisms for the deployment, maintenance, and scaling of applications. Although it may sound more complicated than it actually is, Kubernetes provides an abstraction layer to actually orchestrating experimentation resources (all the way from containers, CPU, memory up to programmable NICs, GPU, even external resources). You can [create a single-node Kubernetes cluster on your computer using a virtual machine](./install-k8s-vm.md) with very reasonable simple requirements like: virtual machine running a deb/rpm-compatible Linux OS, for example Ubuntu or CentOS; 2GB or more of RAM; at least 2 vCPUs; 20GB of disk space; and Internet access.

There are many Kubernetes clusters available for commercial and academic usage. Some commercial examples include:

- [DigitalOcean Kubernetes - DOKS](https://docs.digitalocean.com/products/kubernetes/)

- [Google Kubernetes Engine - GKE](https://cloud.google.com/kubernetes-engine)

- [Amazon Elastic Kubernetes Service - Amazon EKS](https://aws.amazon.com/eks/)

- [Microsoft Azure Kubernetes Service - AKS](https://azure.microsoft.com/es-es/products/kubernetes-service)

For academic/research usage, there are also many projects that offer experimentation resourecs via Kubernetes APIs:

- [Serviço de Testbed RNP - Cluster Nacional](https://ajuda.rnp.br/servico-de-testbeds)

- [MENTORED Testbed for DDoS & IoT & Flexibility](https://portal.mentored.ccsc-research.org)

- [NRP Kubernetes portal](https://portal.nrp-nautilus.io) 

<a name="run-with-docker"/>
## Running with Docker

TODO

<a name="install"/>
## Installation step-by-step

TODO

<a name="helloworld"/>
## Running a Hello World Lab

TODO
