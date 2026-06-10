"""Merkle proof API router."""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter
from slowapi.util import get_remote_address
import hmac

from calculator.merkle_tree import MerkleTree
from storage import STORE

router = APIRouter()

def _client_key(request):
    secret = os.environ.get("FRY_PROXY_KEY", "")
    supplied = request.headers.get("x-fry-proxy-key", "")
    xff = request.headers.get("x-forwarded-for", "")
    if secret and supplied and hmac.compare_digest(supplied, secret) and xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# Decoration-only limiter; RateLimitExceeded is handled by app-level exception handler
_router_limiter = Limiter(key_func=_client_key)

MERKLE_TREE_CACHE_SIZE = int(os.getenv("MERKLE_TREE_CACHE_SIZE", "10"))
_merkle_cache: OrderedDict[int, tuple[Dict[str, Any], MerkleTree, Dict[str, int]]] = OrderedDict()

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_tree(epoch: int) -> Optional[tuple[Dict[str, Any], MerkleTree, Dict[str, int]]]:
    """Load merkle tree for epoch from cache or MongoDB."""
    if epoch in _merkle_cache:
        _merkle_cache.move_to_end(epoch)
        return _merkle_cache[epoch]

    tree_data = STORE.get_merkle_tree(epoch)
    if not tree_data:
        return None

    wallets = tree_data.get("wallets", {})
    wallet_amounts: Dict[str, Dict[str, int]] = {}
    wallet_index: Dict[str, int] = {}

    for addr, data in wallets.items():
        wallet_amounts[addr] = {
            "tfry": data["entitled_tfry"],
            "fnode": data["entitled_fnode"],
        }
        wallet_index[addr] = data["leaf_index"]

    tree, _ = MerkleTree.from_preseed(wallet_amounts)
    result = (tree_data, tree, wallet_index)
    _merkle_cache[epoch] = result
    if len(_merkle_cache) > MERKLE_TREE_CACHE_SIZE:
        _merkle_cache.popitem(last=False)
    return result


@router.get("/api/merkle/proof")
@_router_limiter.limit("60/minute")
def get_merkle_proof(
    request: Request,
    wallet: str = Query(..., description="Algorand wallet address"),
    epoch: int = Query(0, description="Reward epoch"),
) -> Dict[str, Any]:
    """Return merkle proof for a wallet in a given epoch."""
    tree_pair = _get_tree(epoch)
    if tree_pair is None:
        return {"eligible": False}

    tree_data, tree, wallet_index = tree_pair
    if wallet not in wallet_index:
        return {"eligible": False}

    idx = wallet_index[wallet]
    proof = tree.get_proof(idx)
    amounts = tree_data["wallets"][wallet]

    return {
        "eligible": True,
        "leaf_index": idx,
        "proof": proof.to_bytes().hex(),
        "entitled_tfry": amounts["entitled_tfry"],
        "entitled_fnode": amounts["entitled_fnode"],
    }


@router.post("/admin/merkle/tree")
@_router_limiter.limit("10/minute")
def post_merkle_tree(
    request: Request,
    payload: Dict[str, Any],
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Ingest a merkle tree for an epoch."""
    from app import verify_bearer_token_admin

    verify_bearer_token_admin(request, credentials)

    wallets = payload.get("wallets")
    root_hex = payload.get("root")
    if not wallets or not root_hex:
        raise HTTPException(status_code=400, detail="Missing 'wallets' or 'root'")

    wallet_amounts: Dict[str, Dict[str, int]] = {}
    for addr, data in wallets.items():
        wallet_amounts[addr] = {
            "tfry": data["entitled_tfry"],
            "fnode": data["entitled_fnode"],
        }

    tree, _ = MerkleTree.from_preseed(wallet_amounts)
    if tree.root.hex() != root_hex:
        raise HTTPException(status_code=400, detail="Root mismatch: computed root does not match provided root")

    epoch = payload.get("epoch", 0)
    doc = {
        "epoch": epoch,
        "root": root_hex,
        "wallets": wallets,
        "wallet_count": len(wallets),
    }
    STORE.put_merkle_tree(epoch, doc)

    if epoch in _merkle_cache:
        del _merkle_cache[epoch]

    return {"status": "ok", "epoch": epoch, "wallet_count": len(wallets)}
