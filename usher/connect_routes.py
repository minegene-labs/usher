"""The request handler's routes for connecting the apps (connect.py): what to show, and Test. Admins only."""

import logging
from http import HTTPStatus

from . import connect, connect_find

log = logging.getLogger("usher")


class ConnectRoutes:
    def connect_get(self, s):
        """GET /api/connect: each app's saved address, whether a key is saved (never the key) and the addresses where
        something is listening on its usual port."""
        if not s["admin"]:
            return self.err(HTTPStatus.FORBIDDEN, "Only an admin can connect the apps.")
        found = connect_find.suggestions()
        return self.send(200, {"apps": [{**app, "suggestions": found.get(app["id"], [])} for app in connect.describe()]})

    def connect_test(self, s, data):
        """POST /api/connect/test: read one app's version with the address and key given (a blank key means the saved
        one, for the saved address only). Nothing is saved; the key is not returned."""
        if not s["admin"]:
            return self.err(HTTPStatus.FORBIDDEN, "Only an admin can connect the apps.")
        app_id = data.get("app")
        if not isinstance(app_id, str) or app_id not in connect.APPS:
            return self.err(HTTPStatus.BAD_REQUEST, "That request didn't make sense.")
        try:
            result = connect.test(app_id, data.get("url"), data.get("key"), data.get("user"))
        except connect.BadAddress as e:
            return self.err(HTTPStatus.BAD_REQUEST, str(e))
        return self.send(200, result)
