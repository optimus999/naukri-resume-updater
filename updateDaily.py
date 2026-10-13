
import os
import secrets
import sys
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import requests

# Network timeouts: (connection timeout, response timeout)
TIMEOUT = (10, 60)

NAUKRI_COOKIES = os.environ.get("NAUKRI_COOKIES", "").strip()
GOOGLE_DRIVE_FILE_ID = os.environ.get("GOOGLE_DRIVE_FILE_ID", "").strip()
NAUKRI_FORM_KEY = os.environ.get("NAUKRI_FORM_KEY", "").strip()

HEADERS = {
    "accept": "application/json",
    "appid": "105",
    "clientid": "d3skt0p",
    "systemid": "jobseeker",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def parse_cookies(cookie_string):
    """Parse a browser-style cookie string without logging its values."""
    cookies = {}

    for pair in cookie_string.split(";"):
        if "=" not in pair:
            continue

        key, value = pair.strip().split("=", 1)
        key = key.strip()
        value = value.strip()

        if key:
            cookies[key] = value

    return cookies


def download_pdf(file_id):
    """Download the resume and perform basic PDF validation."""
    url = "https://drive.google.com/uc"

    response = requests.get(
        url,
        params={"export": "download", "id": file_id},
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    pdf_bytes = response.content

    # Basic file signature check. This does not validate every PDF structure.
    if not pdf_bytes.startswith(b"%PDF-"):
        raise RuntimeError(
            "Google Drive did not return a PDF. "
            "Check the file ID and download permissions."
        )

    if len(pdf_bytes) < 100:
        raise RuntimeError("The downloaded PDF appears to be empty or incomplete.")

    print(f"[OK] Downloaded PDF ({len(pdf_bytes):,} bytes)")
    return pdf_bytes


def refresh_token(session, refresh_token_value):
    """Attempt to refresh the Naukri access token."""
    response = session.post(
        "https://www.naukri.com/central-login-services/v1/login/token/refresh",
        headers={
            **HEADERS,
            "content-type": "application/json",
        },
        json={"refreshToken": refresh_token_value},
        timeout=TIMEOUT,
    )

    if response.status_code != 200:
        print(f"[WARN] Token refresh returned HTTP {response.status_code}")
        return None

    # The service may set the new token on the response or session cookie jar.
    token = response.cookies.get("nauk_at")

    if not token:
        token = session.cookies.get("nauk_at")

    return token


def get_profile_id(session, token):
    """Retrieve the profile ID for the authenticated account."""
    response = session.get(
        "https://www.naukri.com/cloudgateway-mynaukri/"
        "resman-aggregator-services/v0/users/self/dashboard",
        headers={
            **HEADERS,
            "authorization": f"Bearer {token}",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    data = response.json()
    dashboard = data.get("dashBoard") or {}
    profile_id = dashboard.get("profileId") or data.get("profileId")

    if not profile_id:
        raise RuntimeError(
            "The dashboard response did not contain a profile ID. "
            "Check whether the session is valid and the API response has changed."
        )

    return profile_id


def upload_file(pdf_bytes, filename, form_key):
    """Upload the PDF to Naukri's file service."""
    file_key = "U" + "".join(
        secrets.choice(
            "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )
        for _ in range(13)
    )

    response = requests.post(
        "https://filevalidation.naukri.com/file",
        headers={
            "appid": "105",
            "systemid": "fileupload",
        },
        files={
            "file": (filename, BytesIO(pdf_bytes), "application/pdf"),
        },
        data={
            "formKey": form_key,
            "fileName": filename,
            "fileKey": file_key,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    print(f"[OK] File upload request returned HTTP {response.status_code}")
    return file_key


def attach_to_profile(session, token, profile_id, form_key, file_key):
    """Attach the uploaded file to the Naukri profile."""
    response = session.post(
        "https://www.naukri.com/cloudgateway-mynaukri/"
        f"resman-aggregator-services/v0/users/self/profiles/"
        f"{profile_id}/advResume",
        headers={
            **HEADERS,
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "x-http-method-override": "PUT",
        },
        json={
            "textCV": {
                "formKey": form_key,
                "fileKey": file_key,
                "textCvContent": None,
            }
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    print(
        "[OK] Profile attachment request returned "
        f"HTTP {response.status_code}"
    )


def main():
    if not all(
        [NAUKRI_COOKIES, GOOGLE_DRIVE_FILE_ID, NAUKRI_FORM_KEY]
    ):
        print(
            "[ERROR] Missing one or more required GitHub secrets: "
            "NAUKRI_COOKIES, GOOGLE_DRIVE_FILE_ID, NAUKRI_FORM_KEY"
        )
        sys.exit(1)

    cookies = parse_cookies(NAUKRI_COOKIES)

    token = cookies.get("nauk_at")
    refresh_token_value = cookies.get("nauk_rt")

    if not token and not refresh_token_value:
        print(
            "[ERROR] No nauk_at or nauk_rt cookie was provided. "
            "Refresh the stored authentication cookies."
        )
        sys.exit(1)

    with requests.Session() as session:
        # TLS certificate verification remains enabled by default.
        for name, value in cookies.items():
            session.cookies.set(name, value, domain=".naukri.com")

        if refresh_token_value:
            try:
                new_token = refresh_token(session, refresh_token_value)
            except requests.RequestException as exc:
                print(
                    "[WARN] Token refresh request failed "
                    f"({type(exc).__name__}); trying the existing access token."
                )
                new_token = None

            if new_token:
                token = new_token
                print("[OK] Access token refreshed")
            elif token:
                print("[WARN] Token refresh was unsuccessful; trying existing token")
            else:
                raise RuntimeError(
                    "Token refresh failed and no access token is available."
                )

        if not token:
            raise RuntimeError("No usable access token is available.")

        print("[OK] Authentication token available")

        profile_id = get_profile_id(session, token)
        print("[OK] Profile ID retrieved")

        pdf_bytes = download_pdf(GOOGLE_DRIVE_FILE_ID)

        # Use India-local time for the filename, independent of runner timezone.
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        filename = f"resume_{today_ist.strftime('%d_%b_%Y').lower()}.pdf"

        file_key = upload_file(
            pdf_bytes,
            filename,
            NAUKRI_FORM_KEY,
        )

        attach_to_profile(
            session,
            token,
            profile_id,
            NAUKRI_FORM_KEY,
            file_key,
        )

        print(
            "[OK] Upload and profile attachment requests completed. "
            "Verify the displayed resume and Last updated date on Naukri."
        )


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        # Avoid dumping request objects, headers, or cookie values into logs.
        print(f"[ERROR] Network request failed: {type(exc).__name__}")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        sys.exit(1)