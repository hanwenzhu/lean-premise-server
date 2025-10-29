This is a server that hosts a Lean premise selection service.

The front-end Lean code is at [premise-selection](https://github.com/hanwenzhu/premise-selection). See its README for more context on this premise selector.
This premise selection is developed as part of [LeanHammer](https://github.com/JOSHCLUNE/LeanHammer).

## Deployment

To start the server on a **GPU**, run:

```sh
docker compose -f docker-compose.yaml -f docker-compose.gpu.yaml up
```

To start the server on a **CPU**, run:

```sh
# This prevents out-of-memory errors but sacrifices speed
export MAX_BATCH_TOKENS=16384
docker compose up
```

These commands start a uvicorn server at `0.0.0.0:80`.

To stop the server, you may use:

```sh
docker compose down
```

By default, the Lean premise server will use our periodically extracted data and trained model on Hugging Face, such as [l3lab/lean-premises](https://huggingface.com/datasets/l3lab/lean-premises). (If desired, you may also prepare your own corpus of Mathlib premises and their embeddings, and the embedding model according to the [training script](https://github.com/hanwenzhu/LeanHammer-training), and then specify your corpus in `.env`.)

The configuration for the server is in `.env`. Important variables include:

| Variable | Description |
|----------|-------------|
| `PORT` | The port on `0.0.0.0` for the server to be deployed (default 80) |
| `DATA_REVISION` | The Lean version of Mathlib premises to use (default newest; recall that the server indexes a set of fixed Mathlib premises while also allowing new/non-Mathlib premises to be uploaded) |
| `MODEL_ID` and `MODEL_REVISION` | The version of the model to use (default newest; by convention, `MODEL_REVISION` is the Mathlib Lean version that the model is trained on) |
| `DTYPE` | The precision to use when embedding (trades off between speed and quality) |
| `MAX_BATCH_TOKENS` | The maximum number of tokens in a batch (trades off between memory and speed). Lower this if you run into OOM issues or error code 137. |
| `MAX_NEW_PREMISES` | The maximum number of new premises the user can upload (trades off between speed and usability) |
| `EMBED_SERVICE_MAX_CONCURRENT_INPUTS` | Maximum number of concurrent inputs sent to embedding service (rate limiter) |

#### Updating the model

For each Lean revision, we may extract new Lean data and/or train a new model. After running `scripts/upload.py` in the [training script](https://github.com/hanwenzhu/LeanHammer-training), which uploads the data, model, and pre-computed embeddings to Hugging Face, please update the relevant entries in `.env` (usually `DATA_REVISION` and `MODEL_REVISION` to the Lean version used).

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

## Design choices

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
