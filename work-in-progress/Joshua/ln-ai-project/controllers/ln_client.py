"""
ln_client.py

A safe, explicit wrapper around `lightning-cli` for Core Lightning (CLN).

WHY THIS FILE EXISTS
--------------------
Directly calling `lightning-cli` from agents is error-prone because:
- RPC sockets live in different places per network (mainnet/testnet/regtest)
- CLN error messages are often misleading ("connection refused")
- Command construction gets duplicated everywhere

This class centralizes:
- RPC socket resolution
- Subprocess execution
- Error handling
- JSON parsing

This file DOES NOT implement business logic.
It is a transport + safety layer only.
"""

import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any


class LightningClient:
    """
    LightningClient provides a minimal, safe interface to lightning-cli.

    DESIGN PRINCIPLES
    -----------------
    - Explicit is better than implicit
    - Never assume mainnet
    - Never assume RPC socket location
    - Fail loudly and clearly
    """

    def __init__(
        self,
        lightning_dir: str,
        network: str = "regtest",
        lightning_cli: str = "lightning-cli",
    ):
        """
        Initialize a LightningClient for a specific CLN node.

        PARAMETERS
        ----------
        lightning_dir : str
            Path to the node's lightning directory
            Example: /home/test/ln-ai-project/data/cln1

        network : str
            Bitcoin network in use (default: regtest)

        lightning_cli : str
            Path or command name for lightning-cli binary

        IMPORTANT CLN DETAIL
        --------------------
        In CLN v25+, the RPC socket is NOT located directly in lightning-dir.
        Instead, it lives here:

            <lightning-dir>/<network>/lightning-rpc

        This class resolves that automatically and refuses to proceed if
        the socket does not exist.
        """

        self.lightning_dir = Path(lightning_dir).expanduser().resolve()
        self.network = network
        self.lightning_cli = lightning_cli

        # Construct the expected RPC socket path explicitly
        self.rpc_file = self.lightning_dir / self.network / "lightning-rpc"

        if not self.rpc_file.exists():
            raise FileNotFoundError(
                f"Lightning RPC socket not found:\n"
                f"  {self.rpc_file}\n\n"
                f"Is lightningd running for this node?"
            )

    # ------------------------------------------------------------------
    # INTERNAL HELPER
    # ------------------------------------------------------------------

    def _run(self, args: List[str]) -> Dict[str, Any]:
        """
        Execute a lightning-cli command safely.

        PARAMETERS
        ----------
        args : List[str]
            Arguments passed to lightning-cli
            Example: ["getinfo"]

        RETURNS
        -------
        dict
            Parsed JSON response from lightning-cli

        ERROR HANDLING STRATEGY
        -----------------------
        - Any non-zero exit code raises RuntimeError
        - stderr is surfaced verbatim
        - stdout is parsed as JSON only on success

        This makes failures obvious and debuggable.
        """

        cmd = [
            self.lightning_cli,
            f"--rpc-file={self.rpc_file}",
            *args,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"lightning-cli failed\n"
                f"CMD: {' '.join(cmd)}\n"
                f"STDERR: {result.stderr.strip()}"
            )

        # All lightning-cli commands return JSON on success
        return json.loads(result.stdout)

    # ------------------------------------------------------------------
    # PUBLIC API METHODS
    # ------------------------------------------------------------------

    def get_info(self) -> Dict[str, Any]:
        """
        Get basic node information.

        Equivalent CLI:
            lightning-cli getinfo

        RETURNS
        -------
        dict containing:
        - id
        - alias
        - network
        - blockheight
        - num_peers
        - num_active_channels
        """
        return self._run(["getinfo"])

    def list_peers(self) -> Dict[str, Any]:
        """
        List connected peers.

        Equivalent CLI:
            lightning-cli listpeers
        """
        return self._run(["listpeers"])

    def list_funds(self) -> Dict[str, Any]:
        """
        List on-chain funds and channel balances.

        Equivalent CLI:
            lightning-cli listfunds

        CRITICAL FOR AI:
        - Used to reason about liquidity
        - Used to avoid payment deadlocks
        """
        return self._run(["listfunds"])

    def list_invoices(self) -> Dict[str, Any]:
        """
        List all invoices (paid, unpaid, expired).

        Equivalent CLI:
            lightning-cli listinvoices
        """
        return self._run(["listinvoices"])

    def create_invoice(
        self,
        amount_msat: int,
        label: str,
        description: str,
    ) -> Dict[str, Any]:
        """
        Create a Lightning invoice.

        IMPORTANT CLN RULE
        ------------------
        Invoice labels MUST be unique forever.
        Reusing a label will always fail.

        PARAMETERS
        ----------
        amount_msat : int
            Invoice amount in millisatoshis

        label : str
            Globally unique invoice label

        description : str
            Human-readable memo

        Equivalent CLI:
            lightning-cli invoice <amount_msat> <label> <description>
        """
        return self._run([
            "invoice",
            str(amount_msat),
            label,
            description,
        ])

    def pay_invoice(self, bolt11: str) -> Dict[str, Any]:
        """
        Pay a BOLT11 invoice.

        Equivalent CLI:
            lightning-cli pay <bolt11>

        Lightning enforces:
        - Route finding
        - Liquidity checks
        - Fee limits
        """
        return self._run(["pay", bolt11])

    def delete_invoice(self, label: str, status: str = "unpaid") -> Dict[str, Any]:
        """
        Delete an invoice.

        Used ONLY for recovery/testing.
        Never delete paid invoices in production.

        Equivalent CLI:
            lightning-cli delinvoice <label> <status>
        """
        return self._run(["delinvoice", label, status])
