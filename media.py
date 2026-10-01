"""Upload configured Pancake image URLs and cache content IDs per page."""
import json
import time
import uuid
import logging
from urllib.error import HTTPError
from diagnostics import register_secret, response_text, log_http_error
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger('pancake.media')


def prepare(store, page, token, body):
    if 'photos' not in body:
        return body
    register_secret(token)
    ids = []
    for index, url in enumerate(body['photos'], 1):
        stage = f'page={page} photo={index}/{len(body["photos"])}'
        LOG.info('%s source=%s', stage, url)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'content.pancake.vn':
            raise ValueError('Only configured content.pancake.vn image URLs are supported')
        with store.connect() as db:
            row = db.execute('SELECT content_id FROM media WHERE page=? AND url=?', (page, url)).fetchone()
        if row:
            LOG.info('%s cache_hit content_id=%s', stage, row[0])
            ids.append(row[0])
            continue
        started = time.monotonic()
        try:
            LOG.info('%s download_start', stage)
            with urlopen(url, timeout=20) as response:
                content_type = response.headers.get_content_type()
                image = response.read(10_000_001)
            LOG.info('%s download_ok mime=%s bytes=%s elapsed=%.2fs', stage, content_type, len(image), time.monotonic()-started)
        except HTTPError as error:
            log_http_error(LOG, stage + ' download', error)
            raise
        except Exception:
            LOG.exception('%s download_failed elapsed=%.2fs', stage, time.monotonic()-started)
            raise
        if len(image) > 10_000_000 or content_type not in ('image/jpeg', 'image/png', 'image/webp'):
            raise ValueError(f'{stage} unsupported mime={content_type} bytes={len(image)} (limit 10000000)')
        boundary = uuid.uuid4().hex
        extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[content_type]
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="photo.{extension}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n').encode() + image + f'\r\n--{boundary}--\r\n'.encode()
        endpoint = f'https://pages.fm/api/public_api/v1/pages/{quote(page, safe="")}/upload_contents?' + urlencode({'page_access_token': token})
        # Uploads and message sends are sequential; stay below the API request rate.
        time.sleep(.25)
        request = Request(endpoint, data=data, headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        started = time.monotonic()
        try:
            LOG.info('%s upload_start endpoint=/pages/%s/upload_contents bytes=%s mime=%s', stage, page, len(data), content_type)
            with urlopen(request, timeout=30) as response:
                raw = response.read()
            try:
                uploaded = json.loads(raw)
            except (ValueError, UnicodeError):
                LOG.error('%s upload_invalid_json response=%s', stage, response_text(raw.decode('utf-8', errors='replace')))
                raise
            LOG.info('%s upload_response elapsed=%.2fs response=%s', stage, time.monotonic()-started, response_text(uploaded))
        except HTTPError as error:
            log_http_error(LOG, stage + ' upload', error)
            raise
        except Exception:
            LOG.exception('%s upload_failed elapsed=%.2fs', stage, time.monotonic()-started)
            raise
        # The live API uses `type`; the published schema uses `attachment_type`.
        if not isinstance(uploaded, dict):
            raise ValueError(f'{stage} upload response must be an object; see upload_response')
        media_type = uploaded.get('attachment_type') or uploaded.get('type')
        if (uploaded.get('success') is not True or not isinstance(uploaded.get('id'), str)
                or not uploaded['id'].strip() or media_type != 'PHOTO'):
            raise ValueError(f'{stage} upload schema rejected: expected success=true, nonempty id, type/attachment_type=PHOTO; see upload_response')
        ids.append(uploaded['id'])
        with store.connect() as db:
            db.execute('INSERT OR REPLACE INTO media VALUES(?,?,?)', (page, url, uploaded['id']))
    result = {key: value for key, value in body.items() if key != 'photos'}
    result['content_ids'] = ids
    LOG.info('page=%s media_ready count=%s content_ids=%s', page, len(ids), ids)
    return result
