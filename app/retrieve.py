# WIP script to retrieve premises using the model

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import os
import shutil
import tarfile
from typing import Optional, List, Dict, Union, Literal, Set, Tuple

import numpy as np
import faiss
import httpx
from pydantic import BaseModel
from huggingface_hub import hf_hub_download

from models import Corpus, PremiseSet, Premise

logger = logging.getLogger("uvicorn.error")

DATA_DIR = os.environ["DATA_DIR"]
DATA_REPO = os.environ["DATA_REPO"]
DATA_REVISION = os.environ["DATA_REVISION"]
MODEL_ID = os.environ["MODEL_ID"]
MODEL_REVISION = os.environ["MODEL_REVISION"]
logger.info(f"Downloading Mathlib premises from {DATA_REPO} @ {DATA_REVISION}")
premises_tar_gz = hf_hub_download(
    repo_id=DATA_REPO, repo_type="dataset", revision=DATA_REVISION,
    filename="premises.tar.gz",
    cache_dir=DATA_DIR,
)
MATHLIB_DIR = os.path.join(DATA_DIR, "Mathlib")
logger.info(f"Extracting premises to {MATHLIB_DIR}")
if os.path.exists(MATHLIB_DIR):
    shutil.rmtree(MATHLIB_DIR)
os.makedirs(MATHLIB_DIR, exist_ok=True)
with tarfile.open(premises_tar_gz, "r:gz") as tar:
    tar.extractall(MATHLIB_DIR)
logger.info(f"Downloading pre-computed embeddings from {DATA_REPO} @ {DATA_REVISION}")
model_name = MODEL_ID.split("/")[1]
PRECOMPUTED_EMBEDDINGS_PATH = hf_hub_download(
    repo_id=DATA_REPO, repo_type="dataset", revision=DATA_REVISION,
    filename=os.path.join("embeddings", f"{model_name}-{MODEL_REVISION}.npy"),
    cache_dir=DATA_DIR,
)

EMBED_SERVICE_URL = os.environ["EMBED_SERVICE_URL"]
if os.environ["EMBED_SERVICE_TIMEOUT"]:
    EMBED_SERVICE_TIMEOUT = float(os.environ["EMBED_SERVICE_TIMEOUT"])
else:
    EMBED_SERVICE_TIMEOUT = None
EMBED_SERVICE_MAX_CONCURRENT_INPUTS = int(os.environ["EMBED_SERVICE_MAX_CONCURRENT_INPUTS"])

LRU_CACHE_SIZE = int(os.environ["LRU_CACHE_SIZE"])
MAX_NEW_PREMISES = int(os.environ["MAX_NEW_PREMISES"])
MAX_CLIENT_BATCH_SIZE = int(os.environ["MAX_CLIENT_BATCH_SIZE"])
MAX_K = int(os.environ["MAX_K"])
DTYPE = os.environ["DTYPE"]
assert DTYPE in ["float32", "float16"]

# Get corpus of premises, including their names and serialized expressions
if os.path.isdir(MATHLIB_DIR):
    logger.info(f"Using saved declarations at {MATHLIB_DIR}")
else:
    raise FileNotFoundError(f"Run download_data.py to save data to {MATHLIB_DIR} first")
corpus = Corpus.from_ntp_toolkit(MATHLIB_DIR)

# Build index from corpus embeddings
def build_index(use_precomputed=True) -> faiss.Index:
    if use_precomputed:
        corpus_embeddings = np.load(PRECOMPUTED_EMBEDDINGS_PATH)
    else:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(MODEL_ID, revision=MODEL_REVISION)
        corpus_embeddings = model.encode(
            [premise.to_string() for premise in corpus.premises],
            show_progress_bar=True,
            batch_size=32,
            convert_to_tensor=True,
        )
    assert corpus_embeddings.shape[0] == len(corpus.premises)

    # Build index to search from using FAISS
    index = faiss.IndexFlatIP(corpus_embeddings.shape[1])
    # NOTE: see README
    # if faiss.get_num_gpus() > 0:
    #     logger.info("Using FAISS on GPU")
    #     res = faiss.StandardGpuResources()  # type: ignore
    #     gpu_idx = 0  # TODO
    #     index = faiss.index_cpu_to_gpu(res, gpu_idx, index)  # type: ignore
    # else:
    logger.info("Using FAISS on CPU")
    index.add(corpus_embeddings)  # type: ignore

    return index

index = build_index()  # wrapping in a function for garbage collection


# Classes for retrieval API
class NewPremise(BaseModel):
    name: str
    decl: str

class RetrievalRequest(BaseModel):
    state: str  # str | List[str] is technically possible
    """The pretty-printed goal using `Meta.ppGoal`."""
    imported_modules: Optional[List[str]] = None
    """Deprecated."""
    local_premises: Optional[List[str | int]] = None
    """
    The list of indexes or names of local premises in the context.
    The indexes refer to the index in the list obtained from /indexed-premises.
    If not specified, use all premises from /indexed-premises on the server.
    """
    new_premises: Optional[List[NewPremise]] = None
    """List of new premises in the context."""
    k: int


class LRUCache:
    def __init__(self, maxsize: int):
        self.cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self.maxsize = maxsize

    def __getitem__(self, key: str) -> np.ndarray:
        self.cache.move_to_end(key)
        return self.cache[key]

    def __setitem__(self, key: str, value: np.ndarray):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        while len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)

    def __contains__(self, key: str):
        return key in self.cache

    def __len__(self):
        return len(self.cache)

embedding_cache = LRUCache(maxsize=LRU_CACHE_SIZE)

class EmbedServiceOverloaded(Exception):
    pass

class EmbedServiceLimiter:
    """A basic structure to help cap the number of concurrent input requests
    to the embed service to `max_concurrent_inputs`.

    If more requests are `acquire`d than `max_concurrent_inputs`, an
    `EmbedServiceOverloaded` is thrown (instead of awaiting until availability
    like usual `asyncio.Semaphore`).

    NOTE: This assumes a single uvicorn worker; otherwise the `embed_service_limiter`
    instance will be cloned. (Server concurrency handled by ASGI/`async`.)
    """
    def __init__(self, max_concurrent_inputs: int):
        self.max_concurrent_inputs = max_concurrent_inputs
        self.available_compute = max_concurrent_inputs
        self.lock = asyncio.Lock()
    async def acquire(self, num_inputs: int):
        async with self.lock:
            if num_inputs > self.available_compute:
                logger.warning(f"Embedding service overloaded ({num_inputs} requested, {self.available_compute} available)")
                raise EmbedServiceOverloaded()
            self.available_compute -= num_inputs
    async def release(self, num_inputs: int):
        async with self.lock:
            self.available_compute += num_inputs
            # Sanity check
            self.available_compute = min(self.max_concurrent_inputs, self.available_compute)

embed_service_limiter = EmbedServiceLimiter(EMBED_SERVICE_MAX_CONCURRENT_INPUTS)

@dataclass
class EmbedInput:
    text: str
    should_cache: bool

async def embed_batch(client: httpx.AsyncClient, batch: List[EmbedInput]) -> np.ndarray:
    """Sends a batch to the text-embeddings-inference service, and caches
    computed embeddings in `embedding_cache`."""
    await embed_service_limiter.acquire(len(batch))
    try:
        response = await client.post(
            f"{EMBED_SERVICE_URL}/embed",
            json={"inputs": [input.text for input in batch], "truncate": True},
            timeout=EMBED_SERVICE_TIMEOUT
        )
    finally:
        await embed_service_limiter.release(len(batch))
    response.raise_for_status()
    embeddings = np.array(response.json(), dtype=DTYPE)
    for input, embedding in zip(batch, embeddings):
        if input.should_cache:
            embedding_cache[input.text] = embedding
    return embeddings

async def embed(states: List[str], premises: List[str], batch_sequential: bool = True) -> Tuple[np.ndarray, np.ndarray]:
    if not states:
        raise ValueError("Empty list of states")
    premises_to_embed = []
    premise_embeddings = np.empty((len(premises), index.d), dtype=DTYPE)
    for i, premise in enumerate(premises):
        if premise in embedding_cache:
            premise_embeddings[i] = embedding_cache[premise]
        else:
            premises_to_embed.append((i, premise))

    inputs = [EmbedInput(s, False) for s in states] + [EmbedInput(p, True) for _, p in premises_to_embed]
    logger.info(f"Received {len(states) + len(premises)} inputs; embedding {len(inputs)} texts")

    batch_embeddings_list: List[np.ndarray]
    async with httpx.AsyncClient(timeout=EMBED_SERVICE_TIMEOUT) as client:
        if batch_sequential:
            batch_embeddings_list = []
            for i in range(0, len(inputs), MAX_CLIENT_BATCH_SIZE):
                batch = inputs[i : i + MAX_CLIENT_BATCH_SIZE]
                batch_embeddings = await embed_batch(client, batch)
                batch_embeddings_list.append(batch_embeddings)

        else:
            batch_embeddings_list = await asyncio.gather(*[
                embed_batch(client, inputs[i : i + MAX_CLIENT_BATCH_SIZE])
                for i in range(0, len(inputs), MAX_CLIENT_BATCH_SIZE)
            ])

    packed_embeddings = np.vstack(batch_embeddings_list, dtype=DTYPE)
    assert packed_embeddings.shape == (len(inputs), index.d)

    state_embeddings = packed_embeddings[:len(states)]
    computed_premise_embeddings = packed_embeddings[len(states):]
    for (i, premise), embedding in zip(premises_to_embed, computed_premise_embeddings):
        # embedding_cache[premise] = embedding  # this is done in embed_batch
        premise_embeddings[i] = embedding
    return state_embeddings, premise_embeddings


async def retrieve_premises_core(states: List[str], k: int, new_premises: List[NewPremise], **kwargs):
    # Embed states and new premises
    new_premise_decls = [premise_data.decl for premise_data in new_premises]
    state_embeddings, new_premise_embeddings = await embed(states, new_premise_decls)

    # Retrieve premises from indexed premises
    scoress, indicess = index.search(state_embeddings, k, **kwargs)  # type: ignore
    scored_indexed_premises = [
        [
            # TODO: make this a class
            {"score": score.item(), "name": corpus.premises[i].name}
            for score, i in zip(scores, indices)
            if i >= 0  # FAISS returns -1 with `sel`
        ]
        for scores, indices in zip(scoress, indicess)
    ]

    # Rank new premises
    new_scoress = np.matmul(state_embeddings, new_premise_embeddings.T)
    scored_new_premises = [
        [
            {"score": score.item(), "name": premise_data.name}
            for score, premise_data in zip(scores, new_premises)
        ]
        for scores in new_scoress
    ]

    # Combine indexed and new premises
    scored_premises = [
        sorted(indexed + new, key=lambda p: p["score"], reverse=True)[:k]
        for indexed, new in zip(scored_indexed_premises, scored_new_premises)
    ]

    return scored_premises

async def retrieve_premises(
    states: Union[str, List[str]],
    imported_modules: Optional[List[str]],
    local_premises: Optional[List[str | int]],
    new_premises: List[NewPremise],
    k: int
):
    """Retrieve premises from all indexed premises in:
    indexed premises in local_premises + unindexed premises in new_premises.

    In case of duplicate names, the signature in `new_premises` overrides the signature indexed on the server.
    """
    if k > MAX_K:
        raise ValueError(f"value of k ({k}) exceeds maximum ({MAX_K})")

    # Accessible premises from the state, starting from imported modules
    accessible_premises: Set[str] = set()

    # Legacy support, TODO remove
    if imported_modules is not None:
        imported_modules_set = set(imported_modules)
        for premise in corpus.premises:
            if premise.module in imported_modules_set:
                accessible_premises.add(premise.name)

    if len(new_premises) > MAX_NEW_PREMISES:
        raise ValueError(f"{len(new_premises)} new premises uploaded, exceeding maximum ({MAX_NEW_PREMISES})")

    # Add local_premises to accessible premises
    if local_premises is None:
        accessible_premises = set(corpus.name2premise)
    else:
        for name in local_premises:
            # A new version of the client side optimizes by only sending the index
            # Here we allow both versions
            if isinstance(name, int) and 0 <= name < len(corpus.unfiltered_premises):
                name = corpus.unfiltered_premises[name].name
            if name in corpus.name2premise:
                accessible_premises.add(name)
            else:
                continue  # not raising an error, because the supplied local premises are unfiltered, so might not be in corpus

    # Remove user-uploaded new premises from accessible set, because they override the server-side signature
    for premise_data in new_premises:
        name = premise_data.name
        if name in corpus.name2premise:
            # User-uploaded premise overrides server-side premise
            accessible_premises.remove(name)

    accessible_premise_idxs = [corpus.name2idx[name] for name in accessible_premises]
    # NOTE: the types of faiss Selector, SearchParameters, and Index should align
    sel = faiss.IDSelectorArray(accessible_premise_idxs)  # type: ignore
    kwargs = {}
    kwargs["params"] = faiss.SearchParameters(sel=sel)  # type: ignore

    if isinstance(states, str):
        premises = await retrieve_premises_core([states], k, new_premises, **kwargs)
        return premises[0]
    else:
        premises = await retrieve_premises_core(states, k, new_premises, **kwargs)
        return premises

# original_modules: List[str] = corpus.modules.copy()
added_premises: List[str] = []
async def add_premise_to_corpus_index(premise: Premise):
    """**Permanently** adds a premise to the index (for the current session).
    Warning: this is (as of currently) only intended for testing / easier benchmarking.
    In most cases, the `new_premises` field of /retrieve should be used instead.
    """
    # WARNING: this will override existing premise if their names coincide.
    # For now, only use for tests.
    # WARNING: this is not thread safe -- it relies on the order of corpus = the order of the index.
    # if premise.module in original_modules:
    #     raise ValueError("Added premise is not from a new module")
    if premise.module in corpus.module_to_premises and premise.name in corpus.module_to_premises[premise.module]:
        # We don't add duplicate premises from the same module
        return
    corpus.add_premise(premise)
    async with httpx.AsyncClient(timeout=EMBED_SERVICE_TIMEOUT) as client:
        premise_embedding = await embed_batch(client, [EmbedInput(premise.to_string(), False)])
    index.add(premise_embedding)  # type: ignore
    added_premises.append(premise.name)

# def remove_added_modules():
#     """Remove all new modules added using `add_premise_to_corpus_index`.
#     Warning: same as `add_premise_to_corpus_index`, this is currently for test only.
#     It depends on the client only adding premises from new modules."""
#     for module in corpus.modules:
#         if module not in original_modules:
#             del corpus.module_to_premises[module]
#     corpus.modules = original_modules
