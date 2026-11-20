"""Request signing for the cloud second copy targets, checked against published examples."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from pfsentinel.models.config import AppConfig
from pfsentinel.services.cloud_targets import day_folders_from_paths
from pfsentinel.utils.logging import redact_secrets
from pfsentinel.utils.request_signing import (
    EMPTY_SHA256,
    azure_canonicalized_resource,
    azure_shared_key_authorization,
    azure_string_to_sign,
    encode_query,
    sigv4_authorization,
    sigv4_canonical_request,
    sigv4_signing_key,
    uri_encode,
)
from pfsentinel.utils.xml_parser import PfSenseXMLError, parse_untrusted_xml

# --- AWS Signature Version 4 -------------------------------------------------
#
# Vectors from AWS documentation:
# 1. "Examples of how to derive a signing key for Signature Version 4"
#    (AWS General Reference): secret wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY,
#    20120215, us-east-1, iam.
# 2. The AWS SigV4 test suite (aws-sig-v4-test-suite), case "get-vanilla":
#    AKIDEXAMPLE, 20150830T123600Z, us-east-1, service "service".
# 3. Amazon S3 API Reference, "Examples: Signature Calculations in AWS
#    Signature Version 4" (sig-v4-header-based-auth): GET Object, PUT Object,
#    GET Bucket Lifecycle and Get Bucket (List Objects), with
#    AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY,
#    20130524T000000Z, us-east-1, bucket examplebucket.

S3_DOC = {
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "service": "s3",
    "amz_date": "20130524T000000Z",
}
S3_DOC_HOST = "examplebucket.s3.amazonaws.com"
S3_DOC_SCOPE = "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request"


def test_sigv4_signing_key_matches_the_aws_example() -> None:
    key = sigv4_signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam"
    )
    assert key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_sigv4_test_suite_get_vanilla() -> None:
    headers = {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}
    canonical, signed = sigv4_canonical_request("GET", "/", [], headers, EMPTY_SHA256)
    assert canonical == (
        "GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\n"
        "host;x-amz-date\n" + EMPTY_SHA256
    )
    assert signed == "host;x-amz-date"
    auth = sigv4_authorization(
        "GET",
        "/",
        [],
        headers,
        EMPTY_SHA256,
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="service",
        amz_date="20150830T123600Z",
    )
    assert auth == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_sigv4_s3_get_object_example() -> None:
    headers = {
        "host": S3_DOC_HOST,
        "range": "bytes=0-9",
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": "20130524T000000Z",
    }
    auth = sigv4_authorization("GET", "/test.txt", [], headers, EMPTY_SHA256, **S3_DOC)
    assert auth == (
        f"AWS4-HMAC-SHA256 {S3_DOC_SCOPE}, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_sigv4_s3_put_object_example() -> None:
    payload = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
    assert payload == "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    headers = {
        "date": "Fri, 24 May 2013 00:00:00 GMT",
        "host": S3_DOC_HOST,
        "x-amz-content-sha256": payload,
        "x-amz-date": "20130524T000000Z",
        "x-amz-storage-class": "REDUCED_REDUNDANCY",
    }
    path = uri_encode("/test$file.text", keep_slash=True)
    assert path == "/test%24file.text"
    auth = sigv4_authorization("PUT", path, [], headers, payload, **S3_DOC)
    assert auth.endswith(
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, "
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


@pytest.mark.parametrize(
    ("query", "signature"),
    [
        ([("lifecycle", "")], "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
        (
            [("prefix", "J"), ("max-keys", "2")],
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
)
def test_sigv4_s3_bucket_examples(query: list[tuple[str, str]], signature: str) -> None:
    headers = {
        "host": S3_DOC_HOST,
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": "20130524T000000Z",
    }
    auth = sigv4_authorization("GET", "/", query, headers, EMPTY_SHA256, **S3_DOC)
    assert auth.endswith(f"Signature={signature}")


def test_query_encoding_is_sorted_and_strict() -> None:
    assert encode_query([("prefix", "fw/home-fw/"), ("list-type", "2")]) == (
        "list-type=2&prefix=fw%2Fhome-fw%2F"
    )
    assert uri_encode("a b+#~", keep_slash=False) == "a%20b%2B%23~"


# --- Azure Shared Key ---------------------------------------------------------
#
# Canonicalized resource examples and the string-to-sign layout are from the
# Azure Storage REST API documentation, "Authorize with Shared Key"
# (Shared Key format for 2009-09-19 and later). Microsoft does not publish a
# finished signature for those examples, so the HMAC step is checked against
# an independent computation, with the documented Azurite development
# account key ("Use the Azurite emulator for local Azure Storage development").

AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


@pytest.mark.parametrize(
    ("path", "query", "expected"),
    [
        (
            "/mycontainer",
            [("restype", "container"), ("comp", "metadata")],
            "/myaccount/mycontainer\ncomp:metadata\nrestype:container",
        ),
        (
            "/mycontainer",
            [
                ("restype", "container"),
                ("comp", "list"),
                ("include", "snapshots"),
                ("include", "metadata"),
                ("include", "uncommittedblobs"),
            ],
            "/myaccount/mycontainer\ncomp:list\ninclude:metadata,snapshots,uncommittedblobs\n"
            "restype:container",
        ),
        ("/mycontainer/myblob", [], "/myaccount/mycontainer/myblob"),
    ],
)
def test_azure_canonicalized_resource_examples(
    path: str, query: list[tuple[str, str]], expected: str
) -> None:
    assert azure_canonicalized_resource("myaccount", path, query) == expected


def test_azure_string_to_sign_example() -> None:
    headers = {"x-ms-date": "Sun, 11 Oct 2009 21:49:13 GMT", "x-ms-version": "2009-09-19"}
    resource = azure_canonicalized_resource(
        "myaccount",
        "/mycontainer",
        [("restype", "container"), ("comp", "metadata"), ("timeout", "20")],
    )
    expected = (
        "GET\n"
        + "\n" * 11  # Content-Encoding through Range, all empty
        + "x-ms-date:Sun, 11 Oct 2009 21:49:13 GMT\nx-ms-version:2009-09-19\n"
        + "/myaccount/mycontainer\ncomp:metadata\nrestype:container\ntimeout:20"
    )
    assert azure_string_to_sign("GET", headers, resource) == expected


def test_azure_zero_content_length_is_signed_as_empty() -> None:
    with_zero = azure_string_to_sign("DELETE", {"Content-Length": "0"}, "/a/b")
    without = azure_string_to_sign("DELETE", {}, "/a/b")
    assert with_zero == without
    assert azure_string_to_sign("PUT", {"Content-Length": "12"}, "/a/b").split("\n")[3] == "12"


def test_azure_shared_key_authorization() -> None:
    headers = {
        "x-ms-date": "Fri, 20 Nov 2026 10:00:00 GMT",
        "x-ms-version": "2025-01-05",
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "application/octet-stream",
        "Content-Length": "5",
    }
    auth = azure_shared_key_authorization(
        "devstoreaccount1", AZURITE_KEY, "PUT", "/backups/fw/a%23b.enc", [], headers
    )
    string_to_sign = (
        "PUT\n\n\n5\n\napplication/octet-stream\n\n\n\n\n\n\n"
        "x-ms-blob-type:BlockBlob\nx-ms-date:Fri, 20 Nov 2026 10:00:00 GMT\n"
        "x-ms-version:2025-01-05\n/devstoreaccount1/backups/fw/a%23b.enc"
    )
    digest = hmac.new(
        base64.b64decode(AZURITE_KEY), string_to_sign.encode(), hashlib.sha256
    ).digest()
    assert auth == f"SharedKey devstoreaccount1:{base64.b64encode(digest).decode()}"


# --- Helpers -----------------------------------------------------------------


def test_day_folders_from_object_keys() -> None:
    keys = [
        "home-fw/2020/01/02/020000_#001/a.enc",
        "home-fw/2020/01/02/020000_#001/b.enc",
        "home-fw/2026/11/20/101500_#007/c.enc",
        "home-fw/notes.txt",
        "home-fw/2020/1/02/x/y.enc",
        "home-fw/2020/01/02",
        "other-fw/2020/01/03/x/y.enc",
    ]
    assert day_folders_from_paths("home-fw", keys) == ["home-fw/2020/01/02", "home-fw/2026/11/20"]


def test_redact_secrets_covers_sas_presigned_and_userinfo() -> None:
    text = (
        "HTTPSConnectionPool: url: /c?restype=container&sig=AbC%2Bdef%3D&sv=1 "
        "https://b.s3.example/k?X-Amz-Signature=abcd1234&X-Amz-Credential=AKID%2F2026 "
        "https://alice:s3cret@dav.example/remote.php"
    )
    out = redact_secrets(text)
    for secret in ("AbC%2Bdef%3D", "abcd1234", "AKID%2F2026", "s3cret"):
        assert secret not in out
    assert "sv=1" in out and "dav.example/remote.php" in out


def test_parse_untrusted_xml_rejects_a_doctype() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]><r>&x;</r>'
    with pytest.raises(PfSenseXMLError, match="DOCTYPE"):
        parse_untrusted_xml(evil)
    assert parse_untrusted_xml(b"<r><a>1</a></r>").findtext("a") == "1"


def test_a_0_3_config_still_loads() -> None:
    old = {
        "backup_policy": {
            "second_copy": {
                "enabled": True,
                "kind": "sftp",
                "path": "pfsentinel",
                "host": "nas.lan",
                "port": 22,
                "username": "pfs",
                "key_path": "~/.ssh/id_ed25519",
                "keep_days": 60,
            }
        }
    }
    policy = AppConfig.model_validate_json(json.dumps(old)).backup_policy.second_copy
    assert (policy.kind, policy.host, policy.keep_days) == ("sftp", "nas.lan", 60)
    assert policy.endpoint is None and policy.azure_auth == "sas" and policy.allow_http is False
