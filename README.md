## Server setup

Run

```sh
python download_data.py
```

which downloads Mathlib data in JSONL and pre-computed embeddings for them to `data`.

This also means this should be re-ran for every data or model update.

## Deployment

Set the relevant variables in `.env`. The important ones are: `TEI_VERSION` (for switching to CPU or different GPU backends for Hugging Face text-embeddings-inference); `DTYPE`, `EMBED_SERVICE_MAX_CONCURRENT_INPUTS` (see [.env]).

To start, run

```sh
docker compose up
```

which runs a uvicorn server on `http://0.0.0.0:80`.

## Overview

`docker-compose.yaml` contains two services: `embed` which runs a TEI instance on one GPU;
and `app` which runs a FastAPI ASGI server (single worker), that handles incoming retrieval requests,
determines the state and the premises that need to be embedded (for the premises not already in LRU cache),
and sends these embedding requests to `embed` (while also maintaining a rate-limiter via
`EMBED_SERVICE_MAX_CONCURRENT_INPUTS` so it doesn't send too many requests to `embed`),
and finally uses FAISS to retrieve the premises to return to the user.

The assumption for this server is single GPU, single univorn worker (utilizing ASGI).
In the future, for scaling, I guess one should make sure #GPUs = #`embed` services = #`app` serivces.

#### Misc
Information on faiss-gpu:
* To install both faiss and pytorch on GPU, I recommend `conda install pytorch::faiss-gpu conda-forge::pytorch-gpu` as of April 2025 because `pytorch::pytorch` is discontinued while `conda-forge::faiss-gpu` did not work for me.
* However, preliminary results show that faiss-cpu is just as fast, and faiss-gpu doesn't support selectors which is critical for us. So we use PyPI faiss-cpu instead.
