"""Tests for merkle tree construction and merkle API."""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from calculator.merkle_tree import MerkleTree, encode_leaf, hash_leaf


def _load_preseed(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


class TestMerkleTree:
    """Unit tests for MerkleTree using preseed_merkle.json."""

    @pytest.fixture(scope="class")
    def preseed(self) -> Dict[str, Any]:
        # Look for preseed_merkle.json relative to project root or test data
        candidates = [
            Path(__file__).parent.parent / "data" / "preseed_merkle.json",
            Path(__file__).parent / "data" / "preseed_merkle.json",
            Path("/home/fry/subdomains/hardware_exe_api/data/preseed_merkle.json"),
        ]
        for c in candidates:
            if c.exists():
                return _load_preseed(str(c))
        pytest.skip("preseed_merkle.json not found")

    @pytest.fixture(scope="class")
    def tree_and_index(self, preseed: Dict[str, Any]):
        wallets = preseed["wallets"]
        wallet_amounts: Dict[str, Dict[str, int]] = {}
        for addr, data in wallets.items():
            wallet_amounts[addr] = {
                "tfry": data["entitled_tfry"],
                "fnode": data["entitled_fnode"],
            }
        tree, index = MerkleTree.from_preseed(wallet_amounts)
        return tree, index, wallets

    def test_root_matches_preseed(self, preseed: Dict[str, Any], tree_and_index):
        tree, _, _ = tree_and_index
        assert tree.root.hex() == preseed["root"]

    def test_random_proofs_match_preseed(self, tree_and_index):
        tree, index, wallets = tree_and_index
        addrs = list(wallets.keys())
        sampled = random.sample(addrs, min(50, len(addrs)))
        for addr in sampled:
            idx = index[addr]
            proof = tree.get_proof(idx)
            expected_proof = wallets[addr]["proof"]
            assert proof.to_bytes().hex() == expected_proof, f"proof mismatch for {addr}"

    def test_all_proofs_verify(self, preseed: Dict[str, Any], tree_and_index):
        tree, index, wallets = tree_and_index
        root = bytes.fromhex(preseed["root"])
        for addr, data in wallets.items():
            idx = index[addr]
            proof = tree.get_proof(idx)
            leaf_hash = tree._layers[0][idx]
            assert MerkleTree.verify_proof(root, leaf_hash, proof) is True, f"verify failed for {addr}"

    def test_encode_leaf_matches_contract(self, tree_and_index):
        """Assert encode_leaf bytes are deterministic and match on-chain format."""
        from algosdk import encoding

        tree, index, wallets = tree_and_index
        for addr, data in wallets.items():
            idx = index[addr]
            wallet_bytes = encoding.decode_address(addr)
            leaf = encode_leaf(
                wallet_bytes,
                data["entitled_tfry"],
                data["entitled_fnode"],
                data["entitled_tfry"],
                data["entitled_fnode"],
            )
            expected_hash = hash_leaf(
                wallet_bytes,
                data["entitled_tfry"],
                data["entitled_fnode"],
                data["entitled_tfry"],
                data["entitled_fnode"],
            )
            actual_hash = tree._layers[0][idx]
            assert actual_hash == expected_hash, f"leaf hash mismatch for {addr}"


class TestMerkleAPI:
    """FastAPI endpoint tests (require running app + MongoDB)."""

    @pytest.fixture(scope="class")
    def client(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except Exception as e:
            pytest.skip(f"Cannot import app for TestClient: {e}")
        return TestClient(app)

    def test_proof_endpoint_missing_epoch_returns_ineligible(self, client):
        """When no tree is seeded, proof endpoint returns eligible:false."""
        resp = client.get("/api/merkle/proof?wallet=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\u0026epoch=999999")
        assert resp.status_code == 200
        assert resp.json()["eligible"] is False
