"""RoCE discovery: which RDMA device and GID index NCCL must use.

For a multi-node tensor-parallel instance the all-reduce is the critical path,
and NCCL only takes the RDMA route if three things are true at once. The
control plane can establish all three, and until now it established none:

1. **The container can open the verbs device.** ``--network host`` shares the
   network namespace, not ``/dev`` — the ``uverbs`` character devices simply are
   not there, and the device cgroup would deny them anyway. Without them
   ``ibv_open_device`` fails, NCCL reports zero IB devices and silently uses
   sockets. Setting ``NCCL_IB_HCA`` alone therefore fixes nothing: it is a
   *filter* over an already-enumerated list.
2. **NCCL knows which device and GID to use.** ``NCCL_IB_HCA`` names the device;
   ``NCCL_IB_GID_INDEX`` selects the RoCE v2 GID whose address matches the
   interconnect. Picking the wrong index is the classic RoCE misconfiguration —
   it connects, then hangs or crawls.
3. **The image ships rdma-core.** NCCL dlopens ``libibverbs``; without it the
   fallback is, again, silent.

This module covers (1) and (2) and reports on (3). Everything here is
best-effort by construction: the probe always exits 0 and reports failure in a
``status=`` field, because a node with no RoCE at all must keep serving over
TCP exactly as it does today. A cluster that works must not stop working
because a diagnostic could not run.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass

log = logging.getLogger("spark.roce")

__all__ = ["RoceInfo", "DETECT_SCRIPT", "detect_script_for", "parse_probe", "USABLE_STATUSES"]

# Statuses where NCCL should actually be pointed at the fabric. Anything else
# means "keep using TCP" — degraded, but working, and explained.
USABLE_STATUSES = frozenset({"ok", "ok-fallback"})

# A device path is interpolated into a generated shell script that runs as root.
# Nothing but a real uverbs node may pass, regardless of where the string came
# from — the probe output, a restored backup bundle, or a hand-edited database.
_UVERBS_RE = re.compile(r"\A/dev/infiniband/uverbs[0-9]{1,3}\Z")
_DEV_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,63}\Z")


@dataclass
class RoceInfo:
    """One node's RoCE facts. ``usable`` gates whether we touch NCCL at all."""

    status: str = "unknown"
    device: str | None = None        # e.g. rocep1s0f1
    port: str | None = None          # e.g. 1
    gid_index: str | None = None     # e.g. 3
    hca: str | None = None           # e.g. rocep1s0f1:1  (NCCL_IB_HCA)
    uverbs: str | None = None        # e.g. /dev/infiniband/uverbs1
    uverbs_present: bool = False
    rdma_cm_present: bool = False
    state: str | None = None
    link_layer: str | None = None
    gid_ip: str | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return (
            self.status in USABLE_STATUSES
            and bool(self.hca)
            and self.gid_index is not None
        )

    def summary(self) -> str:
        """One line for a job log — the operator should not have to read sysfs."""
        if self.usable:
            base = f"RoCE ready: {self.hca} GID {self.gid_index}"
            if self.gid_ip:
                base += f" ({self.gid_ip})"
            if not self.uverbs_present:
                base += " — but no verbs char device on the host"
            return base
        return f"RoCE not used ({self.status}): the all-reduce will run over TCP. {self.detail}".strip()


# The probe. POSIX sh, no forks in the hot path, always exits 0.
DETECT_SCRIPT = r"""# ---------------------------------------------------------------------------
# RoCE detection: given the interconnect netdev (and the IPv4 address configured
# on it), find the RDMA device, port, and RoCE v2 GID index that NCCL must use,
# plus the /dev/infiniband char devices that have to be mapped into the
# container for verbs to work at all.
#
# Contract: prints EXACTLY ONE line of space-separated key=value pairs on stdout
# and ALWAYS exits 0. Failures are reported in `status=`, never as an exit code,
# so the caller keeps the diagnosis instead of a bare 502.
#
# Inputs: shell variables IFACE (required) and IPV4 (optional), which the caller
# prepends as two shlex-quoted assignments. SYS/DEV_NODES exist only so the
# script can be unit-tested against a fixture tree.
#
# POSIX sh only: no arrays, no [[ ]], no ${var//}. Every sysfs read is a shell
# builtin redirect (no fork), so scanning a full 255-entry GID table is cheap.
# ---------------------------------------------------------------------------
SYS=${SYS:-/sys}
DEV_NODES=${DEV_NODES:-/dev/infiniband}
IPV4=${IPV4:-}

IBROOT=$SYS/class/infiniband

# --- helpers ---------------------------------------------------------------

# Read the first line of a sysfs attribute into RD_VAL without forking.
# Returns 1 if the file is missing, unreadable (EACCES) or the kernel refuses
# the read (mlx5 returns EINVAL for GID slots that are not populated).
RD_VAL=
rd() {
	RD_VAL=
	[ -f "$1" ] || return 1
	IFS= read -r RD_VAL < "$1" 2>/dev/null || { RD_VAL=; return 1; }
	return 0
}

# Strip leading zeros: printf '%x' 08 is an *error* in bash (invalid octal).
dec() {
	_d=$1
	while :; do
		case $_d in 0[0-9]*) _d=${_d#0} ;; *) break ;; esac
	done
	printf '%s' "$_d"
}

# 10.88.124.33 -> 0a587c21  (the low 32 bits of an IPv4-mapped IPv6 GID)
ip_suffix() {
	_ip=$1
	case $_ip in *.*.*.*) ;; *) return 1 ;; esac
	_o1=${_ip%%.*}
	_r=${_ip#*.}
	_o2=${_r%%.*}
	_r=${_r#*.}
	_o3=${_r%%.*}
	_o4=${_r#*.}
	for _o in "$_o1" "$_o2" "$_o3" "$_o4"; do
		case $_o in '' | *[!0-9]*) return 1 ;; esac
		[ "$_o" -le 255 ] || return 1
	done
	printf '%02x%02x%02x%02x' \
		"$(dec "$_o1")" "$(dec "$_o2")" "$(dec "$_o3")" "$(dec "$_o4")"
}

# "0000:0000:...:0a58:7c21" -> "00000000...0a587c21" (colon-free, lowercase)
norm_gid() { printf '%s' "$1" | tr -d ':' | tr 'ABCDEF' 'abcdef'; }

# "00000000000000000000ffff0a587c21" -> 10.88.124.33 (empty if not IPv4-mapped)
gid_to_ip() {
	case $1 in 00000000000000000000ffff????????) ;; *) return 1 ;; esac
	_h=${1#00000000000000000000ffff}
	_h1=${_h%??????}
	_h=${_h#??}
	_h2=${_h%????}
	_h=${_h#??}
	_h3=${_h%??}
	_h4=${_h#??}
	printf '%d.%d.%d.%d' \
		"$((0x$_h1))" "$((0x$_h2))" "$((0x$_h3))" "$((0x$_h4))"
}

emit() {
	printf 'status=%s iface=%s ip=%s dev=%s port=%s gid_index=%s gid=%s' \
		"$1" "$IFACE" "$IPV4" "$BEST_DEV" "$BEST_PORT" "$BEST_IDX" "$BEST_GID"
	printf ' gid_ip=%s gid_type=%s ndev=%s match=%s state=%s phys_state=%s' \
		"$BEST_GID_IP" "$BEST_TYPE" "$BEST_NDEV" "$MATCH" "$STATE" "$PHYS_STATE"
	printf ' link_layer=%s rate=%s uverbs=%s uverbs_present=%s' \
		"$LINK_LAYER" "$RATE" "$UVERBS" "$UVERBS_PRESENT"
	printf ' rdma_cm_present=%s nccl_ib_hca=%s\n' "$RDMA_CM_PRESENT" "$HCA"
	exit 0
}

BEST_DEV= BEST_PORT= BEST_IDX= BEST_GID= BEST_TYPE= BEST_NDEV= BEST_GID_IP=
MATCH=none STATE= PHYS_STATE= LINK_LAYER= RATE=
UVERBS= UVERBS_PRESENT=0 RDMA_CM_PRESENT=0 HCA=
BEST_RANK=9 SAW_PERM=0 ASSOC=0

# --- validate --------------------------------------------------------------
# A garbled iface must never reach the output line: one stray space would break
# the single-line key=value contract for every field after it.
case $IFACE in '' | *[!A-Za-z0-9._-]*) IFACE=; IPV4=; emit bad-iface ;; esac
case $IPV4 in *[!0-9.]*) IPV4=; emit bad-ip ;; esac

[ -d "$SYS/class/net/$IFACE" ] || emit no-netdev
[ -d "$IBROOT" ] || emit no-rdma

# --- 1. netdev -> RDMA device candidates -----------------------------------
CANDS=
add_cand() {
	case " $CANDS " in *" $1 "*) return 0 ;; esac
	CANDS="$CANDS $1"
	ASSOC=1
}

# (a) forward map: the netdev's PCI function lists the RDMA device(s) it hosts
for p in "$SYS/class/net/$IFACE/device/infiniband/"*; do
	[ -d "$p" ] && add_cand "${p##*/}"
done

# (b) VLAN / bond: the upper netdev has no PCI parent, so descend to lower_*
PHYS=$IFACE
if [ ! -d "$SYS/class/net/$IFACE/device" ]; then
	for lw in "$SYS/class/net/$IFACE/lower_"*; do
		[ -d "$lw" ] || continue
		lower=${lw##*/}
		lower=${lower#lower_}
		PHYS=$lower
		for p in "$SYS/class/net/$lower/device/infiniband/"*; do
			[ -d "$p" ] && add_cand "${p##*/}"
		done
	done
fi

# (c) reverse map: an RDMA device whose PCI function owns this netdev
for d in "$IBROOT"/*; do
	[ -d "$d" ] || continue
	dn=${d##*/}
	[ -d "$d/device/net/$IFACE" ] && add_cand "$dn"
	if [ "$PHYS" != "$IFACE" ] && [ -d "$d/device/net/$PHYS" ]; then
		add_cand "$dn"
	fi
done

# (d) last resort: consider every RDMA device. The GID-table ndev match below
#     is authoritative, so a wrong candidate simply never scores.
if [ -z "$CANDS" ]; then
	for d in "$IBROOT"/*; do
		[ -d "$d" ] && CANDS="$CANDS ${d##*/}"
	done
	[ -n "$CANDS" ] || emit no-rdma
fi

# --- 2. pick the port + GID index ------------------------------------------
# rank 1 = RoCE v2, bound to our netdev, GID encodes IPV4   <- what we want
# rank 2 = RoCE v2, bound to our netdev, other global GID
# rank 3 = RoCE v2, bound to our netdev, link-local (fe80::) only
IPHEX=
[ -n "$IPV4" ] && IPHEX=$(ip_suffix "$IPV4")
WANT=
[ -n "$IPHEX" ] && WANT="00000000000000000000ffff$IPHEX"

for dn in $CANDS; do
	for pd in "$IBROOT/$dn/ports/"*; do
		[ -d "$pd" ] || continue
		pn=${pd##*/}
		case $pn in '' | *[!0-9]*) continue ;; esac
		# Unreadable-but-present state file => running unprivileged.
		if [ -f "$pd/state" ] && [ ! -r "$pd/state" ]; then
			SAW_PERM=1
			continue
		fi
		for gf in "$pd/gids/"*; do
			i=${gf##*/}
			case $i in '' | *[!0-9]*) continue ;; esac
			rd "$gf" || continue
			g=$RD_VAL
			# The GID table is sparse (mlx5 exposes 255 slots); skip the
			# empty ones before paying for the two gid_attrs reads.
			case $g in
			'' | '::' | 0000:0000:0000:0000:0000:0000:0000:0000) continue ;;
			esac
			rd "$pd/gid_attrs/ndevs/$i" || continue
			nd=$RD_VAL
			[ "$nd" = "$IFACE" ] || [ "$nd" = "$PHYS" ] || continue
			rd "$pd/gid_attrs/types/$i" || continue
			ty=$RD_VAL
			ng=$(norm_gid "$g")
			# "RoCE v2" vs "IB/RoCE v1" -- test for v2 explicitly: the v1
			# string also contains the substring "RoCE".
			case $ty in
			*v2*)
				case $ng in
				fe80*) rank=3 ;;
				*) rank=2 ;;
				esac
				if [ -n "$WANT" ]; then
					# The second test covers kernels that print the
					# compressed IPv4-mapped form instead of 8 hex groups.
					if [ "$ng" = "$WANT" ] || [ "$g" = "::ffff:$IPV4" ]; then
						rank=1
					fi
				fi
				gtype=RoCEv2
				;;
			*)
				# Keep v1 as the worst rank rather than dropping it, so a
				# port that is up but configured v1-only still reports which
				# device/port it is instead of a bare "nothing found".
				rank=4
				gtype=RoCEv1
				;;
			esac
			better=0
			if [ "$rank" -lt "$BEST_RANK" ]; then
				better=1
			elif [ "$rank" -eq "$BEST_RANK" ] &&
				[ "$dn" = "$BEST_DEV" ] && [ "$pn" = "$BEST_PORT" ] &&
				[ "$i" -lt "$BEST_IDX" ]; then
				# gids/* globs lexicographically (0,1,10,100,...), so the
				# lowest numeric index is not necessarily seen first.
				better=1
			fi
			if [ "$better" = 1 ]; then
				BEST_RANK=$rank
				BEST_DEV=$dn
				BEST_PORT=$pn
				BEST_IDX=$i
				BEST_GID=$g
				BEST_NDEV=$nd
				BEST_TYPE=$gtype
				BEST_GID_IP=$(gid_to_ip "$ng")
			fi
		done
	done
done

if [ -z "$BEST_DEV" ]; then
	[ "$SAW_PERM" = 1 ] && emit eperm
	[ "$ASSOC" = 1 ] && emit no-gid
	emit no-device
fi

# --- 3. port metadata for the winner ---------------------------------------
PD=$IBROOT/$BEST_DEV/ports/$BEST_PORT
rd "$PD/state" && STATE=${RD_VAL##*: }             # "4: ACTIVE"  -> ACTIVE
rd "$PD/phys_state" && PHYS_STATE=${RD_VAL##*: }   # "5: LinkUp"  -> LinkUp
rd "$PD/link_layer" && LINK_LAYER=$RD_VAL          # Ethernet | InfiniBand
rd "$PD/rate" && RATE=${RD_VAL%% *}                # "200 Gb/sec (4X NDR)" -> 200

# --- 4. the char devices the container needs -------------------------------
for u in "$SYS/class/infiniband_verbs/uverbs"*; do
	[ -d "$u" ] || continue
	rd "$u/ibdev" || continue
	[ "$RD_VAL" = "$BEST_DEV" ] || continue
	UVERBS=$DEV_NODES/${u##*/}
	break
done
if [ -z "$UVERBS" ]; then
	for u in "$IBROOT/$BEST_DEV/device/infiniband_verbs/uverbs"*; do
		[ -d "$u" ] || continue
		UVERBS=$DEV_NODES/${u##*/}
		break
	done
fi
[ -n "$UVERBS" ] && [ -c "$UVERBS" ] && UVERBS_PRESENT=1
[ -c "$DEV_NODES/rdma_cm" ] && RDMA_CM_PRESENT=1

HCA=$BEST_DEV:$BEST_PORT
case $BEST_RANK in
1) MATCH=ip ;;
2) MATCH=global ;;
3) MATCH=linklocal ;;
4) MATCH=v1only ;;
esac

# --- 5. verdict ------------------------------------------------------------
[ "$BEST_RANK" = 4 ] && emit no-rocev2
[ "$LINK_LAYER" = Ethernet ] || emit not-roce
[ "$STATE" = ACTIVE ] || emit link-down
[ "$BEST_RANK" = 1 ] && emit ok
# Only link-local GIDs: the netdev carries no IP-derived GID at all.
[ "$BEST_RANK" = 3 ] && emit no-ip-gid
# A global GID exists but does not encode the address we were given: the node
# record and the wire disagree (wrong qsfp_ip, or the address was added after
# the driver built its GID cache). gid_ip= shows what the port actually carries.
[ -n "$IPV4" ] && emit ip-mismatch
emit ok-fallback
"""


def detect_script_for(iface: str, ipv4: str | None) -> str:
    """The probe with its two inputs bound, safely quoted."""
    return (
        "IFACE=" + shlex.quote(iface or "") + "\n"
        "IPV4=" + shlex.quote(ipv4 or "") + "\n"
        + DETECT_SCRIPT
    )


def parse_probe(stdout: str) -> RoceInfo:
    """Parse the probe's single key=value line. Never raises."""
    info = RoceInfo()
    line = ""
    for candidate in (stdout or "").splitlines():
        if "status=" in candidate:
            line = candidate.strip()
    if not line:
        info.status = "no-output"
        info.detail = _EXPLAIN["no-output"]
        return info

    fields: dict[str, str] = {}
    for token in line.split(" "):
        key, sep, value = token.partition("=")
        if sep:
            fields[key] = value

    info.status = fields.get("status") or "unknown"
    info.state = fields.get("state") or None
    info.link_layer = fields.get("link_layer") or None
    info.gid_ip = fields.get("gid_ip") or None
    info.uverbs_present = fields.get("uverbs_present") == "1"
    info.rdma_cm_present = fields.get("rdma_cm_present") == "1"

    # Validate before keeping. These end up in a root-run shell script and in
    # the container's environment; the probe is ours, but a restored bundle or
    # an edited row is not, and the check costs nothing.
    device = fields.get("dev") or ""
    port = fields.get("port") or ""
    gid = fields.get("gid_index") or ""
    hca = fields.get("nccl_ib_hca") or ""
    uverbs = fields.get("uverbs") or ""

    if _DEV_RE.match(device):
        info.device = device
    if port.isdigit():
        info.port = port
    if gid.isdigit():
        info.gid_index = gid
    if hca and info.device and info.port and hca == f"{info.device}:{info.port}":
        info.hca = hca
    if _UVERBS_RE.match(uverbs):
        info.uverbs = uverbs
    elif uverbs:
        log.info("RoCE probe returned an unusable device path %r; ignoring", uverbs)

    info.detail = _EXPLAIN.get(info.status, "")
    return info


# Every non-usable status gets a sentence naming the fix, because "RoCE not
# used" on its own sends an operator to the wrong place.
_EXPLAIN: dict[str, str] = {
    "no-rdma": "No RDMA subsystem on this node (/sys/class/infiniband is absent).",
    "no-device": "No RDMA device is associated with the interconnect interface.",
    "no-netdev": "The configured interconnect interface does not exist on this node.",
    "no-gid": "The RDMA device has no GID bound to the interconnect interface.",
    "no-rocev2": "The port offers only RoCE v1; NCCL needs a RoCE v2 GID.",
    "no-ip-gid": "The port has only link-local GIDs — no IP is configured on the interconnect.",
    "not-roce": "The port is native InfiniBand, not RoCE over Ethernet.",
    "link-down": "The RDMA port is not ACTIVE — check the QSFP cable and link state.",
    "ip-mismatch": (
        "The port's GID does not encode the interconnect IP recorded for this node. "
        "The node's qsfp_ip and the address actually on the interface disagree."
    ),
    "eperm": "Could not read the RDMA port state (permissions).",
    "bad-iface": "The node has no usable interconnect interface name recorded.",
    "bad-ip": "The node's interconnect IP is not a valid IPv4 address.",
    "no-output": "The RoCE probe returned nothing.",
    "unknown": "The RoCE probe returned an unrecognised status.",
}
