"""Finite, durable controller for Full Manual Adjust execution."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from .manual_adjust_models import AttemptResult
from .manual_adjust_queue import ManualAdjustQueue
from .panel_service import ManualSubmitOutcome


@dataclass(frozen=True)
class ControllerStep:
    state: str
    transaction_id: int | None = None
    detail: str = ""


class ManualAdjustController:
    """Processes one persisted PENDING transaction per ``step`` call.

    It has no Sheet, Validator, AUTO queue, or AUTO database dependency.
    """
    def __init__(self, repository, panel, config: dict, *, executor_id: str | None = None,
                 evidence_dir: str | Path = "screenshots"):
        self.repository = repository
        self.panel = panel
        candidate = config.get("manual_adjust", config) if isinstance(config, dict) else {}
        self.config = candidate if isinstance(candidate, dict) else {}
        self.executor_id = executor_id or str(uuid.uuid4())
        self.cycle_id: str | None = None
        self.queue = None
        self.stop_requested = False
        self.hard_stopped = False
        self.current_transaction = None
        self.evidence_dir = Path(evidence_dir)

    def _preflight(self, cycle_id: str) -> None:
        if self.config.get("execution_enabled", False) is not True:
            raise RuntimeError("Manual Adjust execution is disabled by configuration.")
        if not self.panel.is_alive() or not self.panel.is_attached:
            raise RuntimeError("Panel must be open and explicitly attached.")
        errors = self.repository.validate_cycle_integrity(cycle_id)
        if errors:
            raise RuntimeError("Manual cycle integrity failed: " + "; ".join(errors))

    def start(self, cycle_id: str, *, confirmed: bool) -> None:
        if not confirmed: raise RuntimeError("Operator confirmation is required.")
        self._preflight(cycle_id)
        cycle = self.repository.get_cycle(cycle_id)
        if not cycle or cycle["status"] != "PREVIEW": raise RuntimeError("A frozen PREVIEW cycle is required.")
        self.repository.confirm_and_start(cycle_id, self.executor_id, int(self.config.get("lease_timeout_sec", 120)))
        self._activate(cycle_id)

    def resume(self, cycle_id: str) -> None:
        self._preflight(cycle_id)
        self.repository.resume_cycle(cycle_id, self.executor_id, int(self.config.get("lease_timeout_sec", 120)))
        self._activate(cycle_id)

    def _activate(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id; self.queue = ManualAdjustQueue(self.repository, cycle_id)
        self.stop_requested = False; self.hard_stopped = False

    def heartbeat(self) -> None:
        if not self.cycle_id: raise RuntimeError("no active Manual cycle")
        try: self.repository.heartbeat_cycle(self.cycle_id, self.executor_id)
        except Exception:
            self.hard_stopped = True; raise

    def request_stop(self) -> None:
        self.stop_requested = True

    def shutdown(self) -> str:
        """Cooperatively persist a safe boundary without guessing a submit.

        A live synchronous panel call cannot normally overlap Qt closeEvent.
        If an adapter nevertheless reports a current transaction, its durable
        SUBMITTING evidence is intentionally left for stale recovery.
        """
        self.stop_requested = True
        if not self.cycle_id:
            self._clear_terminal_state()
            return "IDLE"
        cycle = self.repository.get_cycle(self.cycle_id)
        if not cycle or cycle["status"] != "RUNNING":
            state = cycle["status"] if cycle else "IDLE"
            self._clear_terminal_state()
            return state
        if self.current_transaction is not None:
            logging.getLogger(__name__).critical(
                "MANUAL shutdown preserves SUBMITTING cycle=%s transaction=%s; stale recovery required",
                self.cycle_id, self.current_transaction.transaction_id)
            self.hard_stopped = True
            return "HARD_STOPPED"
        try:
            destination = self.repository.evaluate_cycle_destination(
                self.cycle_id, self.executor_id, stopped=True)
        except Exception:
            logging.getLogger(__name__).exception(
                "MANUAL shutdown durability failed cycle=%s; stale recovery required",
                self.cycle_id)
            self.hard_stopped = True
            return "HARD_STOPPED"
        self._clear_terminal_state()
        return destination

    def _clear_terminal_state(self) -> None:
        self.current_transaction = None
        self.queue = None
        self.cycle_id = None
        self.stop_requested = False
        self.hard_stopped = False

    def step(self) -> ControllerStep:
        if not self.cycle_id or self.hard_stopped: return ControllerStep("HARD_STOPPED")
        if self.stop_requested:
            dest = self.repository.evaluate_cycle_destination(self.cycle_id, self.executor_id, stopped=True)
            if dest != "RUNNING": self._clear_terminal_state()
            return ControllerStep(dest)
        try: self.repository.heartbeat_cycle(self.cycle_id, self.executor_id)
        except Exception as exc:
            self.hard_stopped = True; return ControllerStep("HARD_STOPPED", detail=str(exc))
        if not self.panel.is_alive() or not self.panel.is_attached:
            dest = self.repository.evaluate_cycle_destination(self.cycle_id, self.executor_id, stopped=True)
            if dest != "RUNNING": self._clear_terminal_state()
            return ControllerStep(dest, detail="panel unavailable before claim")
        item = self.queue.next_pending() if self.queue else None
        if item is None:
            dest = self.repository.evaluate_cycle_destination(self.cycle_id, self.executor_id)
            if dest != "RUNNING": self._clear_terminal_state()
            return ControllerStep(dest)
        try:
            attempt = self.repository.claim_pending(item.transaction_id, self.executor_id)
        except Exception as exc:
            self.hard_stopped = True; return ControllerStep("HARD_STOPPED", item.transaction_id, str(exc))
        self.current_transaction = item
        attempt_id = attempt["attempt_id"]
        durability_failed = False

        def phase_hook(phase: str) -> None:
            nonlocal durability_failed
            try: self.repository.record_attempt_phase(attempt_id, self.executor_id, phase)
            except Exception:
                durability_failed = True; self.hard_stopped = True; raise

        try:
            result = self.panel.submit_adjustment(item.username, item.adjust_amount,
                                                  str(self.config.get("remark", "MANUAL ADJUST")), phase_hook)
        except Exception as exc:
            # The click boundary is unknowable when an adapter violates its
            # result contract.  Preserve SUBMITTING for stale recovery rather
            # than manufacturing a retryable terminal result.
            self.current_transaction = None
            self.hard_stopped = True
            self._emergency(item, attempt_id, "PANEL_EXCEPTION", None, str(exc))
            return ControllerStep("HARD_STOPPED", item.transaction_id,
                                  "unexpected panel exception")
        self.current_transaction = None
        if durability_failed:
            self._emergency(item, attempt_id, result.phase, result.click_crossed, result.detail)
            return ControllerStep("HARD_STOPPED", item.transaction_id, "phase durability failure")
        mapped = AttemptResult(result.outcome.value)
        try:
            self.repository.finish_attempt(attempt_id, mapped, click_crossed=result.click_crossed,
                                           submission_phase=result.phase, error_detail=result.detail or None,
                                           evidence_detail=result.evidence or None)
        except Exception as exc:
            self.hard_stopped = True
            self._emergency(item, attempt_id, result.phase, result.click_crossed, str(exc))
            return ControllerStep("HARD_STOPPED", item.transaction_id, "attempt result durability failure")
        if result.outcome is ManualSubmitOutcome.UNKNOWN:
            self._emergency(item, attempt_id, result.phase, result.click_crossed, result.detail)
        if not self.panel.is_alive(): self.stop_requested = True
        return ControllerStep(result.outcome.value, item.transaction_id, result.detail)

    def retry_selected(self, cycle_id: str, transaction_ids: list[int], *, confirmed: bool) -> None:
        if not confirmed: raise RuntimeError("Retry confirmation is required.")
        self._preflight(cycle_id)
        self.repository.prepare_failure_retries(cycle_id, transaction_ids)
        self.resume(cycle_id)

    def finalize_with_failures(self, cycle_id: str, *, confirmed: bool) -> None:
        if not confirmed: raise RuntimeError("Finalization confirmation is required.")
        self.repository.finalize_with_failures(cycle_id)

    def reconcile_unknown(self, transaction_id: int, attempt_id: str, outcome: str,
                          *, reconciled_by: str, note: str, evidence: str) -> None:
        self.repository.reconcile_unknown(transaction_id, attempt_id, outcome,
                                          reconciled_by=reconciled_by, note=note, evidence=evidence)

    def recover_stale(self, cycle_id: str) -> str:
        return self.repository.recover_stale_cycle(cycle_id, int(self.config.get("lease_timeout_sec", 120)))

    def _emergency(self, item, attempt_id, phase, click, error) -> None:
        logging.getLogger(__name__).critical("MANUAL emergency cycle=%s transaction=%s attempt=%s user=%s amount=%s phase=%s click=%s error=%s",
            self.cycle_id, item.transaction_id, attempt_id, item.username, item.adjust_amount, phase, click, error)
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            self.panel.screenshot(str(self.evidence_dir / f"manual-emergency-{attempt_id}.png"))
        except Exception: pass
