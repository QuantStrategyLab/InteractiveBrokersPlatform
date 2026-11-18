"""Known-object storage only: no list permission and no raw-object deletion."""
import json
import uuid
from datetime import datetime, timedelta, timezone


class Conflict(RuntimeError):
    pass


class Store:
    def __init__(self, bucket):
        from google.cloud import storage
        self.bucket = storage.Client().bucket(bucket)

    def read(self, key):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        blob = self.bucket.blob(key)
        try:
            blob.reload(timeout=30, retry=None)
            generation = int(blob.generation)
            body = blob.download_as_bytes(if_generation_match=generation, timeout=30, retry=None)
            return body, generation
        except NotFound:
            return None, 0
        except PreconditionFailed:
            raise Conflict('object_changed') from None

    def write(self, key, body, generation=0):
        from google.api_core.exceptions import PreconditionFailed
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(body, if_generation_match=generation, timeout=30, retry=None)
        except PreconditionFailed:
            raise Conflict('object_changed') from None
        return int(blob.generation)

    def json(self, key, default=None):
        body, generation = self.read(key)
        return (json.loads(body) if body is not None else default), generation

    def put(self, key, value, generation=0):
        return self.write(key, json.dumps(value, sort_keys=True, separators=(',', ':')).encode(), generation)

    def immutable(self, key, body):
        try:
            self.write(key, body)
        except Conflict:
            existing, _ = self.read(key)
            if existing != body:
                raise Conflict('immutable_content_mismatch') from None


class Lease:
    """31-minute lease exceeds the 30-minute job kill deadline; CAS fences writers."""
    def __init__(self, store, key, now):
        self.store, self.key, self.owner = store, key, uuid.uuid4().hex
        current, generation = store.json(key, {})
        if current and datetime.fromisoformat(current['expires']) > now:
            raise Conflict('already_running')
        self.value = {'owner': self.owner, 'expires': (now + timedelta(minutes=31)).isoformat()}
        self.generation = store.put(key, self.value, generation)

    def check(self, now):
        value, generation = self.store.json(self.key)
        if value != self.value or generation != self.generation or datetime.fromisoformat(value['expires']) <= now:
            raise Conflict('lease_lost')

    def release(self):
        self.value['expires'] = datetime.now(timezone.utc).isoformat()
        self.store.put(self.key, self.value, self.generation)
