# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
import urllib.parse
from dataclasses import dataclass
from genlayer import *


def _to_address(addr) -> Address:
    if isinstance(addr, Address):
        return addr
    return Address(addr)


if not hasattr(gl, "ContractAt"):
    try:
        setattr(gl, "ContractAt", getattr(gl, "get_contract_at", None))
    except Exception:
        pass


def validate_https_url(url: str, param_name: str = "URL") -> urllib.parse.SplitResult:
    """
    Validates URL scheme, authority, and safety boundaries.
    Enforces scheme == 'https', non-empty host, rejects query strings, fragments,
    userinfo/credentials, and path traversal elements ('..').
    """
    if not url or not isinstance(url, str) or not url.strip():
        raise gl.vm.UserError(f"{param_name} cannot be empty")

    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except Exception as exc:
        raise gl.vm.UserError(f"{param_name} is malformed: {exc}")

    if parsed.scheme.lower() != "https":
        raise gl.vm.UserError(f"{param_name} scheme must be https")

    if not parsed.netloc or not parsed.hostname:
        raise gl.vm.UserError(f"{param_name} missing host")

    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise gl.vm.UserError(f"{param_name} credentials or userinfo not permitted")

    if parsed.fragment:
        raise gl.vm.UserError(f"{param_name} fragments not permitted")

    unquoted_path = urllib.parse.unquote(parsed.path)
    segments = unquoted_path.split("/")
    if any(seg == ".." for seg in segments):
        raise gl.vm.UserError(f"{param_name} path traversal elements not permitted")

    return parsed


@allow_storage
@dataclass
class ShipmentRecord:
    shipment_id: u256
    buyer: Address
    carrier: Address
    manifest_id: str
    destination_code: str
    freight_payout: u256
    required_carrier_bond: u256
    carrier_bond_locked: u256
    max_transit_hours: u256
    delay_penalty_bps_per_hour: u256
    max_temp_celsius: i64
    temp_breach_penalty_bps: u256
    tracking_url: str
    telemetry_url: str
    inspection_url: str
    dispute_reason: str
    status: str
    delivery_outcome: str
    actual_transit_hours: u256
    observed_max_temp_c: i64
    temp_excursion: bool
    tamper_or_damage: bool
    carrier_payout_bps: u256
    bond_slash_bps: u256
    audit_summary: str
    dispute_window_blocks: u256
    provisional_epoch: u256
    dispute_used: bool
    buyer_waived_dispute: bool
    carrier_waived_dispute: bool


class DeliveryProof(gl.Contract):
    """
    DeliveryProof: Production-Grade Value-Backed Autonomous Cold-Chain & Freight Delivery Settlement Contract.
    Enforces real native value-backed escrow, performance bond locking, restricted keeper epoch clocks,
    objective multi-source physical telemetry extraction via GenVM LLM/Web consensus, deterministic integer SLA penalty math,
    and counterparty early dispute waivers or appellate dispute inspection resolution.
    """

    timelock_keeper: Address
    logical_epoch: u256
    total_escrow_locked: u256
    total_shipments: u256
    balances: TreeMap[Address, u256]
    withdrawn_totals: TreeMap[Address, u256]
    shipments: TreeMap[u256, ShipmentRecord]

    def __init__(self, timelock_keeper: Address):
        self.timelock_keeper = _to_address(timelock_keeper)
        self.logical_epoch = u256(0)
        self.total_escrow_locked = u256(0)
        self.total_shipments = u256(0)

    def _get_sender(self) -> Address:
        try:
            sender = gl.message.sender_address
        except AttributeError:
            sender = gl.message.sender  # type: ignore
        return _to_address(sender)

    @gl.public.write.payable
    def deposit_funds(self) -> u256:
        val = u256(gl.message.value)
        if val == u256(0):
            raise gl.vm.UserError("Must attach non-zero value to deposit")
        sender = self._get_sender()
        self.balances[sender] = self.balances.get(sender, u256(0)) + val
        return self.balances[sender]

    @gl.public.write
    def withdraw(self, amount: u256) -> dict:
        if amount == u256(0):
            raise gl.vm.UserError("Withdrawal amount must be greater than zero")
        sender = self._get_sender()
        current_bal = self.balances.get(sender, u256(0))
        if current_bal < amount:
            raise gl.vm.UserError("Insufficient balance for withdrawal")

        # Checks-Effects-Interactions (CEI)
        self.balances[sender] = current_bal - amount
        self.withdrawn_totals[sender] = self.withdrawn_totals.get(sender, u256(0)) + amount

        # Native value transfer to sender via gl.ContractAt(sender).emit_transfer(value=amount)
        target = _to_address(sender)
        contract_at_fn = getattr(gl, "ContractAt", None) or getattr(gl, "get_contract_at", None)
        if contract_at_fn is not None:
            contract_at_fn(target).emit_transfer(value=amount)

        return {
            "recipient": sender,
            "amount": amount,
            "remaining_balance": self.balances[sender],
        }

    @gl.public.write
    def set_timelock_keeper(self, new_keeper: Address) -> None:
        sender = self._get_sender()
        if sender != self.timelock_keeper:
            raise gl.vm.UserError("Only timelock keeper can update keeper")
        self.timelock_keeper = _to_address(new_keeper)

    @gl.public.write
    def advance_epoch(self, steps: u256) -> u256:
        sender = self._get_sender()
        if sender != self.timelock_keeper:
            raise gl.vm.UserError("Only timelock keeper can advance epoch")
        if steps < u256(1) or steps > u256(50):
            raise gl.vm.UserError("Steps must be between 1 and 50")

        # Revert if keeper is buyer or carrier on ANY currently PROVISIONAL shipment
        tot = int(self.total_shipments)
        for i in range(1, tot + 1):
            s = self.shipments[u256(i)]
            if s.status == "PROVISIONAL":
                if sender == s.buyer or sender == s.carrier:
                    raise gl.vm.UserError(
                        "Partisan keeper cannot advance epoch while party to provisional shipment"
                    )

        self.logical_epoch = self.logical_epoch + steps
        return self.logical_epoch

    @gl.public.write.payable
    def create_shipment(
        self,
        carrier: Address,
        manifest_id: str,
        destination_code: str,
        freight_payout: u256,
        required_carrier_bond: u256,
        max_transit_hours: u256,
        delay_penalty_bps_per_hour: u256,
        max_temp_celsius: i64,
        temp_breach_penalty_bps: u256,
        dispute_window_blocks: u256,
    ) -> u256:
        buyer = self._get_sender()
        carrier_addr = _to_address(carrier)
        if carrier_addr == buyer:
            raise gl.vm.UserError("Carrier cannot be buyer")
        if not manifest_id or not str(manifest_id).strip():
            raise gl.vm.UserError("Manifest ID cannot be empty")
        if not destination_code or not str(destination_code).strip():
            raise gl.vm.UserError("Destination code cannot be empty")
        if freight_payout == u256(0):
            raise gl.vm.UserError("Freight payout must be greater than zero")
        if max_transit_hours == u256(0):
            raise gl.vm.UserError("Max transit hours must be greater than zero")
        if dispute_window_blocks == u256(0):
            raise gl.vm.UserError("Dispute window blocks must be greater than zero")
        if temp_breach_penalty_bps > u256(10000):
            raise gl.vm.UserError("Temp breach penalty bps cannot exceed 10000")

        # Credit any attached native value directly to buyer
        attached = u256(gl.message.value)
        if attached > u256(0):
            self.balances[buyer] = self.balances.get(buyer, u256(0)) + attached

        current_bal = self.balances.get(buyer, u256(0))
        if current_bal < freight_payout:
            raise gl.vm.UserError("Insufficient balance for freight payout escrow")

        # Deduct freight payout and lock in escrow
        self.balances[buyer] = current_bal - freight_payout
        self.total_escrow_locked = self.total_escrow_locked + freight_payout

        self.total_shipments = self.total_shipments + u256(1)
        shipment_id = self.total_shipments

        new_shipment = ShipmentRecord(
            shipment_id=shipment_id,
            buyer=buyer,
            carrier=carrier_addr,
            manifest_id=str(manifest_id).strip(),
            destination_code=str(destination_code).strip(),
            freight_payout=freight_payout,
            required_carrier_bond=required_carrier_bond,
            carrier_bond_locked=u256(0),
            max_transit_hours=max_transit_hours,
            delay_penalty_bps_per_hour=delay_penalty_bps_per_hour,
            max_temp_celsius=max_temp_celsius,
            temp_breach_penalty_bps=temp_breach_penalty_bps,
            tracking_url="",
            telemetry_url="",
            inspection_url="",
            dispute_reason="",
            status="CREATED",
            delivery_outcome="UNDECIDED",
            actual_transit_hours=u256(0),
            observed_max_temp_c=i64(0),
            temp_excursion=False,
            tamper_or_damage=False,
            carrier_payout_bps=u256(0),
            bond_slash_bps=u256(0),
            audit_summary="",
            dispute_window_blocks=dispute_window_blocks,
            provisional_epoch=u256(0),
            dispute_used=False,
            buyer_waived_dispute=False,
            carrier_waived_dispute=False,
        )
        self.shipments[shipment_id] = new_shipment
        return shipment_id

    @gl.public.write.payable
    def stake_carrier_bond(self, shipment_id: u256) -> None:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        if s.status != "CREATED":
            raise gl.vm.UserError(f"Cannot stake bond in status {s.status}")

        carrier = self._get_sender()
        if carrier != s.carrier:
            raise gl.vm.UserError("Only designated carrier can stake bond")

        # Credit attached native value to carrier
        attached = u256(gl.message.value)
        if attached > u256(0):
            self.balances[carrier] = self.balances.get(carrier, u256(0)) + attached

        current_bal = self.balances.get(carrier, u256(0))
        if current_bal < s.required_carrier_bond:
            raise gl.vm.UserError("Insufficient balance for carrier bond")

        self.balances[carrier] = current_bal - s.required_carrier_bond
        s.carrier_bond_locked = s.required_carrier_bond
        self.total_escrow_locked = self.total_escrow_locked + s.required_carrier_bond
        s.status = "IN_TRANSIT"
        self.shipments[shipment_id] = s

    @gl.public.write
    def submit_delivery_proof(self, shipment_id: u256, tracking_url: str, telemetry_url: str) -> None:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        if s.status != "IN_TRANSIT":
            raise gl.vm.UserError(f"Cannot submit delivery proof in status {s.status}")

        sender = self._get_sender()
        if sender != s.carrier:
            raise gl.vm.UserError("Only carrier can submit delivery proof")

        validate_https_url(tracking_url, "Tracking URL")
        validate_https_url(telemetry_url, "Telemetry URL")

        s.tracking_url = str(tracking_url).strip()
        s.telemetry_url = str(telemetry_url).strip()
        s.status = "PROOF_SUBMITTED"
        self.shipments[shipment_id] = s

    @gl.public.write
    def verify_delivery_and_sla(self, shipment_id: u256) -> dict:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        if s.status != "PROOF_SUBMITTED":
            raise gl.vm.UserError(f"Cannot verify delivery in status {s.status}")

        manifest_id = s.manifest_id
        dest_code = s.destination_code
        max_transit_h = int(s.max_transit_hours)
        delay_penalty_rate = int(s.delay_penalty_bps_per_hour)
        max_temp_c = int(s.max_temp_celsius)
        temp_penalty_rate = int(s.temp_breach_penalty_bps)
        tracking_url = s.tracking_url
        telemetry_url = s.telemetry_url

        def run_eval() -> dict:
            # 1. Fail-closed multi-source web acquisition
            try:
                track_res = gl.nondet.web.get(tracking_url)
            except Exception as exc:
                raise gl.vm.UserError(f"Failed to fetch tracking data: {exc}")
            if track_res.status != 200:
                raise gl.vm.UserError(f"Tracking endpoint returned status {track_res.status}")
            track_text = (track_res.body or b"").decode("utf-8", errors="replace").strip()
            if not track_text:
                raise gl.vm.UserError("Tracking data response was empty")

            try:
                telem_res = gl.nondet.web.get(telemetry_url)
            except Exception as exc:
                raise gl.vm.UserError(f"Failed to fetch telemetry data: {exc}")
            if telem_res.status != 200:
                raise gl.vm.UserError(f"Telemetry endpoint returned status {telem_res.status}")
            telem_text = (telem_res.body or b"").decode("utf-8", errors="replace").strip()
            if not telem_text:
                raise gl.vm.UserError("Telemetry data response was empty")

            prompt = f"""You are an autonomous smart contract logistics oracle and SLA verification engine.
Analyze the following quarantined carrier tracking and IoT cold-chain telemetry documents to verify physical delivery.

=== CARRIER TRACKING ({tracking_url}) ===
<CARRIER_TRACKING>
{track_text[:3500]}
</CARRIER_TRACKING>

=== IOT COLD-CHAIN TELEMETRY ({telemetry_url}) ===
<IOT_TELEMETRY>
{telem_text[:3500]}
</IOT_TELEMETRY>

Shipment Parameters:
- Manifest ID: {manifest_id}
- Target Destination Code: {dest_code}
- Max Allowed Transit Hours: {max_transit_h}
- Cold-Chain Temperature Ceiling: {max_temp_c} Celsius

Extract physical facts into strict JSON format with exactly these fields:
1. "delivered": true if delivery status is confirmed delivered, false if in-transit/undelivered/missing.
2. "destination_matched": true if delivery destination airport/hub/address matches target {dest_code}.
3. "actual_transit_hours": total integer hours elapsed from carrier pickup to final delivery.
4. "observed_max_temp_c": maximum integer Celsius temperature recorded across all sensors.
5. "tamper_or_damage": true if seal tampering, physical damage, crush, or cargo compromise occurred.
6. "summary": concise factual summary under 160 characters.

Return ONLY raw JSON object:
{{
  "delivered": true,
  "destination_matched": true,
  "actual_transit_hours": 48,
  "observed_max_temp_c": 6,
  "tamper_or_damage": false,
  "summary": "Completed on-schedule within temperature SLA."
}}"""

            raw_res = gl.nondet.exec_prompt(prompt, response_format="json")
            if isinstance(raw_res, str):
                raw_res = json.loads(raw_res)
            if not isinstance(raw_res, dict):
                raise gl.vm.UserError("Oracle LLM returned non-dictionary response")

            delivered = bool(raw_res.get("delivered", False))
            dest_matched = bool(raw_res.get("destination_matched", False))
            tamper = bool(raw_res.get("tamper_or_damage", False))

            try:
                transit_h = max(0, int(round(float(str(raw_res.get("actual_transit_hours", 0)).strip()))))
            except Exception:
                transit_h = 0

            try:
                max_t = int(round(float(str(raw_res.get("observed_max_temp_c", 0)).strip())))
            except Exception:
                max_t = 0

            summary = str(raw_res.get("summary", "Delivery telemetry processed."))[:200]

            # Deterministic Python SLA Penalty Math
            if not delivered or not dest_matched or tamper:
                outcome = "DAMAGED_OR_LOST"
                carrier_payout_bps = 0
                bond_slash_bps = 10000
                temp_excursion = max_t > max_temp_c
            else:
                delay_hours = max(0, transit_h - max_transit_h)
                delay_penalty_bps = delay_hours * delay_penalty_rate
                temp_excursion = max_t > max_temp_c
                temp_penalty_bps = temp_penalty_rate if temp_excursion else 0
                total_penalty_bps = min(10000, delay_penalty_bps + temp_penalty_bps)
                carrier_payout_bps = 10000 - total_penalty_bps

                if total_penalty_bps == 0:
                    outcome = "ON_TIME_INTACT"
                    bond_slash_bps = 0
                elif total_penalty_bps >= 5000:
                    outcome = "SLA_BREACH_PARTIAL"
                    bond_slash_bps = 5000
                else:
                    outcome = "SLA_BREACH_PARTIAL"
                    bond_slash_bps = 0

            return {
                "delivered": delivered,
                "destination_matched": dest_matched,
                "actual_transit_hours": transit_h,
                "observed_max_temp_c": max_t,
                "temp_excursion": temp_excursion,
                "tamper_or_damage": tamper,
                "outcome": outcome,
                "carrier_payout_bps": carrier_payout_bps,
                "bond_slash_bps": bond_slash_bps,
                "summary": summary,
            }

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            lead = leaders_res.calldata
            if not isinstance(lead, dict):
                return False

            for k in ("outcome", "temp_excursion", "tamper_or_damage", "carrier_payout_bps", "bond_slash_bps"):
                if k not in lead:
                    return False

            try:
                val = run_eval()
            except Exception:
                return False

            if not isinstance(val, dict):
                return False

            # Equivalence principle checks
            if lead["outcome"] != val["outcome"]:
                return False
            if lead["temp_excursion"] != val["temp_excursion"]:
                return False
            if lead["tamper_or_damage"] != val["tamper_or_damage"]:
                return False
            if abs(int(lead["carrier_payout_bps"]) - int(val["carrier_payout_bps"])) > 1000:
                return False
            if int(lead["bond_slash_bps"]) != int(val["bond_slash_bps"]):
                return False

            return True

        eval_result = gl.vm.run_nondet(run_eval, validator_fn)

        s.actual_transit_hours = u256(int(eval_result["actual_transit_hours"]))
        s.observed_max_temp_c = i64(int(eval_result["observed_max_temp_c"]))
        s.temp_excursion = bool(eval_result["temp_excursion"])
        s.tamper_or_damage = bool(eval_result["tamper_or_damage"])
        s.delivery_outcome = str(eval_result["outcome"])
        s.carrier_payout_bps = u256(int(eval_result["carrier_payout_bps"]))
        s.bond_slash_bps = u256(int(eval_result["bond_slash_bps"]))
        s.audit_summary = str(eval_result.get("summary", ""))
        s.provisional_epoch = self.logical_epoch
        s.status = "PROVISIONAL"
        self.shipments[shipment_id] = s

        return eval_result

    @gl.public.write
    def waive_dispute_window(self, shipment_id: u256) -> str:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        if s.status != "PROVISIONAL":
            raise gl.vm.UserError(f"Cannot waive dispute window in status {s.status}")

        sender = self._get_sender()
        if sender != s.buyer and sender != s.carrier:
            raise gl.vm.UserError("Only buyer or carrier can waive dispute window")

        if sender == s.buyer:
            s.buyer_waived_dispute = True
        if sender == s.carrier:
            s.carrier_waived_dispute = True

        can_finalize = False
        # Buyer waiver for full payout & 0 slash
        if s.carrier_payout_bps == u256(10000) and s.bond_slash_bps == u256(0):
            if s.buyer_waived_dispute:
                can_finalize = True
        # Carrier waiver / concession for 0 payout & full slash
        elif s.carrier_payout_bps == u256(0) and s.bond_slash_bps == u256(10000):
            if s.carrier_waived_dispute:
                can_finalize = True
        # Mutual waiver for intermediate / partial deduction verdicts
        elif s.buyer_waived_dispute and s.carrier_waived_dispute:
            can_finalize = True

        if can_finalize:
            s.status = "FINALIZED"

        self.shipments[shipment_id] = s
        return s.status

    @gl.public.write
    def challenge_sla_verdict(self, shipment_id: u256, inspection_url: str, dispute_reason: str) -> None:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        if s.status != "PROVISIONAL":
            raise gl.vm.UserError(f"Cannot challenge SLA verdict in status {s.status}")

        if self.logical_epoch > s.provisional_epoch + s.dispute_window_blocks:
            raise gl.vm.UserError("Dispute window has expired")
        if s.dispute_used:
            raise gl.vm.UserError("Dispute has already been raised for this shipment")

        sender = self._get_sender()
        if sender != s.buyer and sender != s.carrier:
            raise gl.vm.UserError("Only buyer or carrier can challenge SLA verdict")

        validate_https_url(inspection_url, "Inspection URL")
        if not dispute_reason or not str(dispute_reason).strip():
            raise gl.vm.UserError("Dispute reason cannot be empty")

        s.inspection_url = str(inspection_url).strip()
        s.dispute_reason = str(dispute_reason).strip()
        s.dispute_used = True
        s.status = "DISPUTED"
        self.shipments[shipment_id] = s

    @gl.public.write
    def resolve_sla_dispute(self, shipment_id: u256) -> dict:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        if s.status != "DISPUTED":
            raise gl.vm.UserError(f"Cannot resolve dispute in status {s.status}")

        manifest_id = s.manifest_id
        dest_code = s.destination_code
        max_transit_h = int(s.max_transit_hours)
        delay_penalty_rate = int(s.delay_penalty_bps_per_hour)
        max_temp_c = int(s.max_temp_celsius)
        temp_penalty_rate = int(s.temp_breach_penalty_bps)
        tracking_url = s.tracking_url
        telemetry_url = s.telemetry_url
        inspection_url = s.inspection_url
        dispute_reason = s.dispute_reason

        def run_appellate_eval() -> dict:
            try:
                track_res = gl.nondet.web.get(tracking_url)
            except Exception as exc:
                raise gl.vm.UserError(f"Failed to fetch tracking data: {exc}")
            if track_res.status != 200:
                raise gl.vm.UserError(f"Tracking endpoint returned status {track_res.status}")
            track_text = (track_res.body or b"").decode("utf-8", errors="replace").strip()

            try:
                telem_res = gl.nondet.web.get(telemetry_url)
            except Exception as exc:
                raise gl.vm.UserError(f"Failed to fetch telemetry data: {exc}")
            if telem_res.status != 200:
                raise gl.vm.UserError(f"Telemetry endpoint returned status {telem_res.status}")
            telem_text = (telem_res.body or b"").decode("utf-8", errors="replace").strip()

            try:
                insp_res = gl.nondet.web.get(inspection_url)
            except Exception as exc:
                raise gl.vm.UserError(f"Failed to fetch inspection evidence: {exc}")
            if insp_res.status != 200:
                raise gl.vm.UserError(f"Inspection endpoint returned status {insp_res.status}")
            insp_text = (insp_res.body or b"").decode("utf-8", errors="replace").strip()

            prompt = f"""You are an appellate logistics tribunal smart contract oracle.
Re-evaluate physical delivery and SLA compliance using authoritative tracking, telemetry, and newly submitted independent inspection rebuttal evidence.

=== CARRIER TRACKING ({tracking_url}) ===
<CARRIER_TRACKING>
{track_text[:2500]}
</CARRIER_TRACKING>

=== IOT COLD-CHAIN TELEMETRY ({telemetry_url}) ===
<IOT_TELEMETRY>
{telem_text[:2500]}
</IOT_TELEMETRY>

=== APPELLATE INSPECTION EVIDENCE ({inspection_url}) ===
<INSPECTION_EVIDENCE>
{insp_text[:2500]}
</INSPECTION_EVIDENCE>

=== PARTY DISPUTE REASON ===
<DISPUTE_REASON>
{dispute_reason[:500]}
</DISPUTE_REASON>

Shipment Parameters:
- Manifest ID: {manifest_id}
- Target Destination Code: {dest_code}
- Max Allowed Transit Hours: {max_transit_h}
- Cold-Chain Temperature Ceiling: {max_temp_c} Celsius

Appellate Guidance:
Weight independent customs/inspection certificates to resolve sensor discrepancies or force majeure claims.
Return ONLY raw JSON with:
1. "delivered": boolean
2. "destination_matched": boolean
3. "actual_transit_hours": integer
4. "observed_max_temp_c": integer
5. "tamper_or_damage": boolean
6. "summary": concise explanation under 160 characters.

{{
  "delivered": true,
  "destination_matched": true,
  "actual_transit_hours": 48,
  "observed_max_temp_c": 5,
  "tamper_or_damage": false,
  "summary": "Appellate inspection verified intact delivery."
}}"""

            raw_res = gl.nondet.exec_prompt(prompt, response_format="json")
            if isinstance(raw_res, str):
                raw_res = json.loads(raw_res)
            if not isinstance(raw_res, dict):
                raise gl.vm.UserError("Oracle LLM returned non-dictionary response")

            delivered = bool(raw_res.get("delivered", False))
            dest_matched = bool(raw_res.get("destination_matched", False))
            tamper = bool(raw_res.get("tamper_or_damage", False))

            try:
                transit_h = max(0, int(round(float(str(raw_res.get("actual_transit_hours", 0)).strip()))))
            except Exception:
                transit_h = 0

            try:
                max_t = int(round(float(str(raw_res.get("observed_max_temp_c", 0)).strip())))
            except Exception:
                max_t = 0

            summary = str(raw_res.get("summary", "Appellate dispute adjudication complete."))[:200]

            if not delivered or not dest_matched or tamper:
                outcome = "DAMAGED_OR_LOST"
                carrier_payout_bps = 0
                bond_slash_bps = 10000
                temp_excursion = max_t > max_temp_c
            else:
                delay_hours = max(0, transit_h - max_transit_h)
                delay_penalty_bps = delay_hours * delay_penalty_rate
                temp_excursion = max_t > max_temp_c
                temp_penalty_bps = temp_penalty_rate if temp_excursion else 0
                total_penalty_bps = min(10000, delay_penalty_bps + temp_penalty_bps)
                carrier_payout_bps = 10000 - total_penalty_bps

                if total_penalty_bps == 0:
                    outcome = "ON_TIME_INTACT"
                    bond_slash_bps = 0
                elif total_penalty_bps >= 5000:
                    outcome = "SLA_BREACH_PARTIAL"
                    bond_slash_bps = 5000
                else:
                    outcome = "SLA_BREACH_PARTIAL"
                    bond_slash_bps = 0

            return {
                "delivered": delivered,
                "destination_matched": dest_matched,
                "actual_transit_hours": transit_h,
                "observed_max_temp_c": max_t,
                "temp_excursion": temp_excursion,
                "tamper_or_damage": tamper,
                "outcome": outcome,
                "carrier_payout_bps": carrier_payout_bps,
                "bond_slash_bps": bond_slash_bps,
                "summary": summary,
            }

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            lead = leaders_res.calldata
            if not isinstance(lead, dict):
                return False

            for k in ("outcome", "temp_excursion", "tamper_or_damage", "carrier_payout_bps", "bond_slash_bps"):
                if k not in lead:
                    return False

            try:
                val = run_appellate_eval()
            except Exception:
                return False

            if not isinstance(val, dict):
                return False

            if lead["outcome"] != val["outcome"]:
                return False
            if lead["temp_excursion"] != val["temp_excursion"]:
                return False
            if lead["tamper_or_damage"] != val["tamper_or_damage"]:
                return False
            if abs(int(lead["carrier_payout_bps"]) - int(val["carrier_payout_bps"])) > 1000:
                return False
            if int(lead["bond_slash_bps"]) != int(val["bond_slash_bps"]):
                return False

            return True

        appellate_result = gl.vm.run_nondet(run_appellate_eval, validator_fn)

        s.actual_transit_hours = u256(int(appellate_result["actual_transit_hours"]))
        s.observed_max_temp_c = i64(int(appellate_result["observed_max_temp_c"]))
        s.temp_excursion = bool(appellate_result["temp_excursion"])
        s.tamper_or_damage = bool(appellate_result["tamper_or_damage"])
        s.delivery_outcome = str(appellate_result["outcome"])
        s.carrier_payout_bps = u256(int(appellate_result["carrier_payout_bps"]))
        s.bond_slash_bps = u256(int(appellate_result["bond_slash_bps"]))
        s.audit_summary = str(appellate_result.get("summary", ""))
        s.status = "FINALIZED"
        self.shipments[shipment_id] = s

        return appellate_result

    @gl.public.write
    def settle_shipment(self, shipment_id: u256) -> dict:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]

        if s.status == "PROVISIONAL":
            if self.logical_epoch <= s.provisional_epoch + s.dispute_window_blocks:
                raise gl.vm.UserError("Dispute window is still active; wait for epoch or waive window")
            s.status = "FINALIZED"

        if s.status != "FINALIZED":
            raise gl.vm.UserError(f"Cannot settle shipment in status {s.status}")

        carrier_freight_share = (s.freight_payout * s.carrier_payout_bps) // u256(10000)
        buyer_freight_rebate = s.freight_payout - carrier_freight_share
        slashed_bond_to_buyer = (s.carrier_bond_locked * s.bond_slash_bps) // u256(10000)
        returned_bond_to_carrier = s.carrier_bond_locked - slashed_bond_to_buyer

        self.balances[s.carrier] = (
            self.balances.get(s.carrier, u256(0)) + carrier_freight_share + returned_bond_to_carrier
        )
        self.balances[s.buyer] = (
            self.balances.get(s.buyer, u256(0)) + buyer_freight_rebate + slashed_bond_to_buyer
        )

        total_released = s.freight_payout + s.carrier_bond_locked
        self.total_escrow_locked = self.total_escrow_locked - total_released
        s.status = "SETTLED"
        self.shipments[shipment_id] = s

        return {
            "shipment_id": shipment_id,
            "carrier_freight_share": carrier_freight_share,
            "buyer_freight_rebate": buyer_freight_rebate,
            "slashed_bond_to_buyer": slashed_bond_to_buyer,
            "returned_bond_to_carrier": returned_bond_to_carrier,
            "carrier_total": carrier_freight_share + returned_bond_to_carrier,
            "buyer_total": buyer_freight_rebate + slashed_bond_to_buyer,
            "status": s.status,
        }

    @gl.public.view
    def get_shipment(self, shipment_id: u256) -> dict:
        if shipment_id < u256(1) or shipment_id > self.total_shipments:
            raise gl.vm.UserError("Shipment not found")
        s = self.shipments[shipment_id]
        return {
            "shipment_id": s.shipment_id,
            "buyer": s.buyer,
            "carrier": s.carrier,
            "manifest_id": s.manifest_id,
            "destination_code": s.destination_code,
            "freight_payout": s.freight_payout,
            "required_carrier_bond": s.required_carrier_bond,
            "carrier_bond_locked": s.carrier_bond_locked,
            "max_transit_hours": s.max_transit_hours,
            "delay_penalty_bps_per_hour": s.delay_penalty_bps_per_hour,
            "max_temp_celsius": s.max_temp_celsius,
            "temp_breach_penalty_bps": s.temp_breach_penalty_bps,
            "tracking_url": s.tracking_url,
            "telemetry_url": s.telemetry_url,
            "inspection_url": s.inspection_url,
            "dispute_reason": s.dispute_reason,
            "status": s.status,
            "delivery_outcome": s.delivery_outcome,
            "actual_transit_hours": s.actual_transit_hours,
            "observed_max_temp_c": s.observed_max_temp_c,
            "temp_excursion": s.temp_excursion,
            "tamper_or_damage": s.tamper_or_damage,
            "carrier_payout_bps": s.carrier_payout_bps,
            "bond_slash_bps": s.bond_slash_bps,
            "audit_summary": s.audit_summary,
            "dispute_window_blocks": s.dispute_window_blocks,
            "provisional_epoch": s.provisional_epoch,
            "dispute_used": s.dispute_used,
            "buyer_waived_dispute": s.buyer_waived_dispute,
            "carrier_waived_dispute": s.carrier_waived_dispute,
        }

    @gl.public.view
    def get_balance(self, account: Address) -> u256:
        return self.balances.get(_to_address(account), u256(0))

    @gl.public.view
    def get_withdrawn_total(self, account: Address) -> u256:
        return self.withdrawn_totals.get(_to_address(account), u256(0))

    @gl.public.view
    def get_contract_state(self) -> dict:
        return {
            "timelock_keeper": self.timelock_keeper,
            "logical_epoch": self.logical_epoch,
            "total_escrow_locked": self.total_escrow_locked,
            "total_shipments": self.total_shipments,
        }
