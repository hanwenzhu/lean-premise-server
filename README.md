This is a server that hosts a Lean premise selection service.

The front-end Lean code is at [premise-selection](https://github.com/hanwenzhu/premise-selection). See its README for more context on this premise selector.
This premise selection is developed as part of [LeanHammer](https://github.com/JOSHCLUNE/LeanHammer).

## Deployment

Before deployment, prepare the corpus of Mathlib premises and their embeddings, and the embedding model according to the [training script](https://github.com/hanwenzhu/LeanHammer-training).

Set the relevant variables in `.env`. The important ones are

* `DATA_REVISION`: the revision (i.e. Lean version) of Mathlib premises to use (recall that the server indexes a set of fixed Mathlib premises while also allowing new/non-Mathlib premises to be uploaded)
* `MODEL_ID` and `MODEL_REVISION`: the version of the model to use (by convention, `MODEL_REVISION` is the Mathlib Lean version that the model is trained on)
* `TEI_VERSION` for switching to CPU or different GPU backends for Hugging Face text-embeddings-inference
* `DTYPE` the precision to use when embedding
* `EMBED_SERVICE_MAX_CONCURRENT_INPUTS` (see `.env`)

See `.env` for other configurations.

To start, run

```sh
docker compose up
```

which runs a uvicorn server on `http://0.0.0.0:80`.

## Overview

`docker-compose.yaml` contains two services:

* `embed` runs a Hugging Face text-embeddings-inference (TEI) instance on one GPU;
* `app` runs a FastAPI ASGI server, that handles incoming retrieval requests,
determines the state and the premises that need to be embedded,
sends these embedding requests to `embed`,
and finally uses FAISS to retrieve the premises to return to the user.

`app` uses a single uvicorn worker and handles concurrency using ASGI and `async`/`await`.
`app` maintains a LRU cache of the embeddings of new premises, so it only
relays to `embed` the requests of new premises it has not seen before.
`app` also maintains a rate-limiter via
`EMBED_SERVICE_MAX_CONCURRENT_INPUTS` so it doesn't send too many requests to `embed`.

## Development

Most design decisions for the code are subject to improvement or refactoring. These include but are not limited to:
* Choice of using FAISS (CPU)
* Choice of using Hugging Face text-embeddings-inference
* Optimization (LRU cache) and rate-limiting code
* Using a single GPU (versus multiple GPUs or no GPUs)
* Pipeline from starting training to obtaining a new model and precomputed embeddings to server update (TODO documentation on this)
* Choice of the numbers in `.env`

The assumption for this server is single GPU, single univorn worker (utilizing ASGI).
In the future, if there are multiple GPUs, I guess one should make sure #GPUs = #`embed` services = #`app` serivces,
and/or use some Docker swarm / Kubernetes setup,
but this has not been tried yet.

#### Misc
Information on faiss-gpu:
* To install both faiss and pytorch on GPU, I recommend `conda install pytorch::faiss-gpu conda-forge::pytorch-gpu` as of April 2025 because `pytorch::pytorch` is discontinued while `conda-forge::faiss-gpu` did not work for me.
* However, preliminary results show that faiss-cpu is just as fast, and faiss-gpu doesn't support selectors which is critical for us. So we use PyPI faiss-cpu instead.
