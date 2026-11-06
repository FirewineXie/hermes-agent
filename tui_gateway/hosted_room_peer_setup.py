"""Shared RoomLink setup for legacy and canonical room owners."""

import os


def ready_room_link(profile: str) -> tuple[dict, dict]:
    """Return a truthful target policy and catalog only with durable API run storage."""
    from gateway.hosted_room_execution_policy import execution_policy_mapping
    from gateway.hosted_room_peer import gateway_room_grant_secret
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    store = RunIdempotencyStore()
    try:
        if not store.durable:
            raise ValueError("durable run idempotency storage is required")
    finally:
        store.close()
    gateway_room_grant_secret()
    policy = execution_policy_mapping(target_profile=profile)
    return policy, catalog_for_profile(profile, policy)


def catalog_for_profile(profile: str, execution_policy: dict) -> dict:
    """Advertise the same direct RoomLink shape on both gateway control paths."""
    from gateway.hosted_room_peer import PROTOCOL_VERSION, local_catalog_mapping
    from gateway.hosted_rooms import local_authority_gateway_id

    return local_catalog_mapping(
        installation_id=local_authority_gateway_id(), protocol_versions=(PROTOCOL_VERSION,),
        link_modes=("direct",), text=True, attachments=False, target_profile=profile,
        execution_policy=execution_policy)


def invite_peer_room(db_path, params: dict, profile: str, execution_policy: dict) -> dict:
    """Issue a member-scoped grant and reserve that member before returning it."""
    from gateway.hosted_room_peer import (
        decode_room_grant, gateway_room_grant_secret, issue_room_grant)
    from gateway.hosted_rooms import local_authority_gateway_id, reserve_peer_room

    ttl = float(params.get("ttl_seconds", 3600))
    if not 60 <= ttl <= 24 * 60 * 60:
        raise ValueError("ttl_seconds must be between 60 and 86400")
    installation_id = local_authority_gateway_id()
    secret = gateway_room_grant_secret()
    grant = issue_room_grant(
        secret, grant_id=str(params.get("grant_id") or f"grant-{os.urandom(16).hex()}"),
        room_id=str(params.get("room_id") or ""),
        home_install_id=str(params.get("home_install_id") or ""),
        authority_gateway_id=str(params.get("authority_gateway_id") or ""),
        authority_epoch=int(params.get("authority_epoch") or 0),
        member_id=str(params.get("member_id") or ""), target_install_id=installation_id,
        target_profile=profile, execution_policy_digest=execution_policy["policy_digest"],
        ttl_seconds=ttl)
    claims = decode_room_grant(secret, grant, permission="status")
    reserve_peer_room(db_path, claims=claims,
                      expires_at=float(claims.get("status_expires_at", claims["expires_at"])))
    catalog = catalog_for_profile(profile, execution_policy)
    return {"grant": grant, "target_profile": profile, "catalog": catalog,
            "endpoint": catalog["endpoint"]}


def revoke_peer_room(db_path, params: dict, profile: str) -> dict:
    """Revoke only a grant issued for this installation and profile."""
    from gateway.hosted_room_peer import decode_room_grant, gateway_room_grant_secret
    from gateway.hosted_rooms import local_authority_gateway_id, revoke_room_grant_scope

    claims = decode_room_grant(
        gateway_room_grant_secret(), str(params.get("grant") or ""), permission="status")
    if (claims["target_profile"] != profile
            or claims["target_install_id"] != local_authority_gateway_id()):
        raise ValueError("room grant target does not match this profile")
    revoke_room_grant_scope(db_path, claims=claims,
                            expires_at=float(claims.get("status_expires_at", claims["expires_at"])))
    return {"revoked": True}


def register_peer_route(service, params: dict) -> dict:
    """Probe one target grant and publish its route only after the scope matches the room."""
    from gateway.hosted_room_peer import (
        GatewayRoomCatalog, PROTOCOL_VERSION, validate_room_link_url)
    from gateway.hosted_rooms import local_authority_gateway_id, room_state
    from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
    from tui_gateway.hosted_room_peer_transport import PeerMemberRoute

    target_url, transport_security = validate_room_link_url(params.get("target_url"))
    catalog = GatewayRoomCatalog.from_mapping(params.get("catalog"))
    if PROTOCOL_VERSION not in catalog.protocol_versions:
        raise ValueError(f"target does not support RoomLink protocol v{PROTOCOL_VERSION}")
    if "direct" not in catalog.link_modes:
        raise ValueError("target does not support a direct RoomLink")
    target_profile = str(params.get("target_profile") or "")
    grant = str(params.get("grant") or "")
    client = PeerRunsHTTPClient(base_url=target_url, api_key="", receipt_db_path=service.db_path)
    probe = client.probe(grant=grant)
    if GatewayRoomCatalog.from_mapping(probe.get("catalog")) != catalog:
        raise ValueError("target capability catalog changed during setup")
    room_id = str(params.get("room_id") or "")
    member_id = str(params.get("member_id") or "")
    home_install_id = local_authority_gateway_id()
    home_room = room_state(service.db_path, room_id=room_id)
    expected_scope = {
        "room_id": room_id, "home_install_id": home_install_id,
        "authority_gateway_id": home_room.get("authority_gateway_id"),
        "member_id": member_id, "target_profile": target_profile}
    if (any(probe.get(k) != v for k, v in expected_scope.items())
            or int(probe.get("authority_epoch") or 0)
            != int(home_room.get("authority_epoch") or 0)):
        raise ValueError("room grant scope does not match this route")
    route = PeerMemberRoute(
        home_install_id=home_install_id, member_id=member_id,
        target_install_id=catalog.installation_id, target_profile=target_profile,
        capability_digest=catalog.catalog_digest,
        execution_policy_digest=catalog.execution_policy.policy_digest,
        cancellation_scope_id=str(
            params.get("cancellation_scope_id") or f"cancel-{params.get('room_id') or ''}"),
        trace_id=str(params.get("trace_id") or f"trace-{os.urandom(16).hex()}"), grant=grant)
    service.register_peer_route(
        room_id=room_id, member_id=member_id, route=route, client=client, target_url=target_url,
        catalog=catalog)
    return {
        "registered": True, "mode": "direct", "transport_security": transport_security,
        "target_install_id": catalog.installation_id, "target_profile": target_profile}
