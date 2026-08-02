"""The operator picks the certificate lifetime; these are the rails.

A lifetime is not a free parameter. Too short and the portal becomes an outage
generator — OpenBao seals for a patch window and inference stops before anyone
is awake. Too long and there is no security benefit left, because this hop has
no revocation at all: ingress-nginx emits no CRL or OCSP directive on the
upstream path, so revoking a certificate in OpenBao changes precisely nothing
until it expires. Lifetime IS the revocation mechanism.

So the number is the operator's to choose and the schedule around it is not.
Everything else — when renewal starts, when a failing renewal becomes an
alert, how much retry headroom exists — is derived, so choosing a lifetime
never means also reasoning about a second number.
"""

from __future__ import annotations

import pytest

from app.services.pki import (
    DEFAULT_TTL_HOURS,
    MAX_TTL_HOURS,
    MIN_TTL_HOURS,
    lifetime_policy,
    normalise_fqdn,
    validate_ttl_hours,
)


def test_the_default_is_seven_days():
    """Chosen because the two obvious answers are both wrong: 24h makes
    OpenBao a hard dependency of inference on a 12-hour fuse, 30d leaves a
    month of undetectable interception on a hop with no revocation."""
    assert validate_ttl_hours(None) == DEFAULT_TTL_HOURS == 168.0


def test_an_operator_choice_is_honoured():
    assert validate_ttl_hours(24) == 24.0
    assert validate_ttl_hours(720) == 720.0


def test_a_lifetime_too_short_to_renew_is_refused_not_clamped():
    """Silently issuing six hours to someone who asked for one gives them a
    system whose behaviour does not match its configuration."""
    with pytest.raises(ValueError) as e:
        validate_ttl_hours(1)
    assert "too short" in str(e.value)
    assert "6 hours" in str(e.value)


def test_the_refusal_explains_the_consequence_not_just_the_rule():
    """An operator who is told 'invalid value' picks another number at random.
    One who is told what breaks picks a defensible one."""
    with pytest.raises(ValueError) as short:
        validate_ttl_hours(0.5)
    assert "headroom" in str(short.value)

    with pytest.raises(ValueError) as long:
        validate_ttl_hours(MAX_TTL_HOURS + 1)
    assert "no revocation" in str(long.value)


def test_nonsense_values_are_rejected():
    for bad in ("soon", None if False else float("inf"), float("nan"), -5):
        with pytest.raises(ValueError):
            validate_ttl_hours(bad)


# --- the derived schedule -------------------------------------------------

def test_renewal_starts_with_a_third_of_the_lifetime_left():
    p = lifetime_policy(168)
    assert p.renew_after_hours == pytest.approx(112.0)     # 4.67 days in
    assert p.retry_window_hours == pytest.approx(56.0)     # 2.3 days of retries


def test_the_schedule_scales_with_the_chosen_lifetime():
    """An operator who picks 24h gets 8h of headroom and one who picks 30d gets
    10 days, without either being asked for a second number."""
    for ttl in (24, 168, 720):
        p = lifetime_policy(ttl)
        assert p.retry_window_hours == pytest.approx(ttl / 3.0)
        assert p.renew_after_hours < p.warn_after_hours < ttl


def test_warning_comes_after_renewal_starts_but_before_expiry():
    """A single failed renewal is normal and must not page anyone; running out
    of window is not normal."""
    p = lifetime_policy(168)
    assert p.renew_after_hours < p.warn_after_hours
    assert p.warn_after_hours < p.ttl_hours


def test_every_permitted_lifetime_has_a_usable_retry_window():
    """The floor exists to guarantee this. At the minimum the window must still
    exceed the time a node reboot plus a renewal retry can take."""
    for ttl in (MIN_TTL_HOURS, 12, 24, 168, 720, MAX_TTL_HOURS):
        assert lifetime_policy(ttl).retry_window_hours >= MIN_TTL_HOURS / 3.0


def test_the_ttl_is_rendered_as_a_go_duration_for_openbao():
    assert lifetime_policy(168).as_bao_ttl() == "168h"
    assert lifetime_policy(24).as_bao_ttl() == "24h"


# --- node identity --------------------------------------------------------

def test_a_node_needs_a_fully_qualified_name():
    """The cluster proxy verifies with X509_check_host against dNSName SANs.
    A bare hostname cannot resolve from the cluster, and the failure would look
    like an upstream connect error rather than anything naming the cause."""
    with pytest.raises(ValueError) as e:
        normalise_fqdn("dgx-md-01")
    assert "fully-qualified" in str(e.value)


def test_fqdns_are_normalised():
    assert normalise_fqdn("DGX-MD-01.Example.NET.") == "dgx-md-01.example.net"
    assert normalise_fqdn("  dgx-md-02.example.net  ") == "dgx-md-02.example.net"


def test_no_name_is_allowed():
    """An existing fleet has none until an operator fills them in; that must
    not be an error, it must mean 'this node cannot use node certificates yet'."""
    assert normalise_fqdn(None) is None
    assert normalise_fqdn("   ") is None


def test_an_ip_address_is_not_a_node_identity():
    """X509_check_host skips iPAddress SANs entirely, so an IP is inert on this
    path no matter what the certificate contains."""
    with pytest.raises(ValueError):
        normalise_fqdn("10.0.0.11")
