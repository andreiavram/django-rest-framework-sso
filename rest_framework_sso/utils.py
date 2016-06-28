# coding: utf-8
from __future__ import absolute_import, unicode_literals

import os
from datetime import datetime

import jwt
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import six
from django.utils.translation import gettext_lazy as _
from jwt.exceptions import MissingRequiredClaimError, InvalidIssuerError, InvalidTokenError, InvalidKeyError
from rest_framework import exceptions
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key, load_pem_public_key, load_ssh_public_key
)
from cryptography.hazmat.backends import default_backend

from rest_framework_sso import claims
from rest_framework_sso.settings import api_settings


def create_session_payload(session_token, user, **kwargs):
    return {
        claims.TOKEN: claims.TOKEN_SESSION,
        claims.SESSION_ID: session_token.pk,
        claims.USER_ID: user.pk,
        claims.EMAIL: user.email,
    }


def create_authorization_payload(session_token, user, **kwargs):
    return {
        claims.TOKEN: claims.TOKEN_AUTHORIZATION,
        claims.SESSION_ID: session_token.pk,
        claims.USER_ID: user.pk,
        claims.EMAIL: user.email,
        claims.SCOPES: [],
    }


def encode_jwt_token(payload):
    if payload.get(claims.TOKEN) not in (claims.TOKEN_SESSION, claims.TOKEN_AUTHORIZATION):
        raise RuntimeError('Unknown token type')

    if not payload.get(claims.ISSUER):
        if api_settings.IDENTITY is not None:
            payload[claims.ISSUER] = api_settings.IDENTITY
        else:
            raise RuntimeError('IDENTITY must be specified in settings')

    if not payload.get(claims.AUDIENCE):
        if payload.get(claims.TOKEN) == claims.TOKEN_SESSION and api_settings.SESSION_AUDIENCE is not None:
            payload[claims.AUDIENCE] = api_settings.SESSION_AUDIENCE
        elif payload.get(claims.TOKEN) == claims.TOKEN_AUTHORIZATION and api_settings.AUTHORIZATION_AUDIENCE is not None:
            payload[claims.AUDIENCE] = api_settings.AUTHORIZATION_AUDIENCE
        elif api_settings.IDENTITY is not None:
            payload[claims.AUDIENCE] = [api_settings.IDENTITY]
        else:
            raise RuntimeError('SESSION_AUDIENCE must be specified in settings')

    if not payload.get(claims.EXPIRATION_TIME):
        if payload.get(claims.TOKEN) == claims.TOKEN_SESSION and api_settings.SESSION_EXPIRATION is not None:
            payload[claims.EXPIRATION_TIME] = datetime.utcnow() + api_settings.SESSION_EXPIRATION
        elif payload.get(claims.TOKEN) == claims.TOKEN_AUTHORIZATION and api_settings.AUTHORIZATION_EXPIRATION is not None:
            payload[claims.EXPIRATION_TIME] = datetime.utcnow() + api_settings.AUTHORIZATION_EXPIRATION

    if not payload.get(claims.ISSUED_AT):
        payload[claims.ISSUED_AT] = datetime.utcnow()

    if payload[claims.ISSUER] not in api_settings.PRIVATE_KEYS:
        raise RuntimeError('Private key for specified issuer was not found in settings')

    private_key, key_id = get_private_key_and_key_id(issuer=payload[claims.ISSUER])

    headers = {
        claims.KEY_ID: key_id,
    }

    return jwt.encode(
        payload=payload,
        key=private_key,
        algorithm=api_settings.ENCODE_ALGORITHM,
        headers=headers,
        json_encoder=DjangoJSONEncoder,
    ).decode('utf-8')


def decode_jwt_token(token):
    unverified_header = jwt.get_unverified_header(token)
    unverified_claims = jwt.decode(token, verify=False)

    if unverified_header.get(claims.KEY_ID):
        unverified_key_id = six.text_type(unverified_header.get(claims.KEY_ID))
    else:
        unverified_key_id = None

    if claims.ISSUER not in unverified_claims:
        raise MissingRequiredClaimError(claims.ISSUER)

    unverified_issuer = six.text_type(unverified_claims[claims.ISSUER])

    if api_settings.ACCEPTED_ISSUERS is not None and unverified_issuer not in api_settings.ACCEPTED_ISSUERS:
        raise InvalidIssuerError('Invalid issuer')

    public_key, key_id = get_public_key_and_key_id(issuer=unverified_issuer, key_id=unverified_key_id)

    options = {
        'verify_exp': api_settings.VERIFY_EXPIRATION,
        'verify_aud': True,
        'verify_iss': True,
    }

    payload = jwt.decode(
        jwt=token,
        key=public_key,
        verify=api_settings.VERIFY_SIGNATURE,
        algorithms=api_settings.DECODE_ALGORITHMS or [api_settings.ENCODE_ALGORITHM],
        options=options,
        leeway=api_settings.EXPIRATION_LEEWAY,
        audience=api_settings.IDENTITY,
        issuer=unverified_issuer,
    )

    if payload.get(claims.TOKEN) not in (claims.TOKEN_SESSION, claims.TOKEN_AUTHORIZATION):
        raise InvalidTokenError('Unknown token type')
    if payload.get(claims.ISSUER) != api_settings.IDENTITY and payload.get(claims.TOKEN) != claims.TOKEN_AUTHORIZATION:
        raise InvalidTokenError('Only authorization tokens are accepted from other issuers')
    if not payload.get(claims.SESSION_ID):
        raise MissingRequiredClaimError('Session ID is missing.')
    if not payload.get(claims.USER_ID):
        raise MissingRequiredClaimError('User ID is missing.')

    return payload


def read_key_file(file_name):
    if api_settings.KEY_STORE_ROOT:
        file_path = os.path.abspath(os.path.join(api_settings.KEY_STORE_ROOT, file_name))
    else:
        file_path = os.path.abspath(file_name)
    return open(file_path, 'rt').read()


def get_key_id(file_name):
    suffixes = ['.pem']
    for suffix in suffixes:
        if file_name.lower().endswith(suffix):
            return file_name[:-len(suffix)]
    return file_name


def get_key_file_name(keys, issuer, key_id=None):
    if not keys.get(issuer):
        raise InvalidKeyError('No keys defined for the given issuer')
    issuer_keys = keys.get(issuer)
    if isinstance(issuer_keys, (str, six.text_type)):
        issuer_keys = [issuer_keys]
    if key_id:
        issuer_keys = [ik for ik in issuer_keys if key_id in (ik, get_key_id(ik))]
    if len(issuer_keys) < 1:
        raise InvalidKeyError('No key matches the given key_id')
    return issuer_keys[0]


def get_private_key_and_key_id(issuer, key_id=None):
    file_name = get_key_file_name(keys=api_settings.PRIVATE_KEYS, issuer=issuer, key_id=key_id)
    file_data = read_key_file(file_name=file_name)
    key = load_pem_private_key(file_data, password=None, backend=default_backend())
    return key, get_key_id(file_name=file_name)


def get_public_key_and_key_id(issuer, key_id=None):
    file_name = get_key_file_name(keys=api_settings.PUBLIC_KEYS, issuer=issuer, key_id=key_id)
    file_data = read_key_file(file_name=file_name)
    key = load_pem_public_key(file_data, backend=default_backend())
    return key, get_key_id(file_name=file_name)


def authenticate_payload(payload):
    from rest_framework_sso.models import SessionToken

    user_model = get_user_model()

    if api_settings.VERIFY_SESSION_TOKEN:
        try:
            session_token = SessionToken.objects.\
                active().\
                select_related('user').\
                get(pk=payload.get(claims.SESSION_ID), user_id=payload.get(claims.USER_ID))
            user = session_token.user
        except SessionToken.DoesNotExist:
            raise exceptions.AuthenticationFailed(_('Invalid token.'))
    else:
        try:
            user = user_model.objects.get(pk=payload.get(claims.USER_ID))
        except user_model.DoesNotExist:
            raise exceptions.AuthenticationFailed(_('Invalid token.'))

    if not user.is_active:
        raise exceptions.AuthenticationFailed(_('User inactive or deleted.'))

    return user
