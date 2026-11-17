from ellalon_sdk import EllalonModule

m = EllalonModule(version="0.1.0")
app = m.app          # add HTTP routes with @app.get(...); exposed at /modules/my-module/


@m.command("ping")   # callable from C&C: POST /api/commands/my-module {"command":"ping"}
async def ping(args):
    return {"pong": True}


@m.on_broadcast("camera.changed")
async def cameras_changed(payload):
    cams = await m.cameras()
    m.status["cameras"] = len(cams)


@m.background
async def worker():
    import asyncio
    while True:
        await m.publish("my-module.tick", {"hello": "world"})
        await asyncio.sleep(60)
