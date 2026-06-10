"""Pydantic schemas for the merkle proof API."""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, Field


class WalletProofData(BaseModel):
    leaf_index: int
    proof: str
    entitled_tfry: int
    entitled_fnode: int


class MerkleTreePayload(BaseModel):
    root: str
    wallets: Dict[str, WalletProofData]


class ProofResponse(BaseModel):
    eligible: bool
    leaf_index: int | None = None
    proof: str | None = None
    entitled_tfry: int | None = None
    entitled_fnode: int | None = None


class TreeIngestResponse(BaseModel):
    status: str
    epoch: int
    wallet_count: int
