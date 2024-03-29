import asyncio
import click
import functools
import traceback
from aiohttp import web
from aiohttp_session import setup as setup_session, get_session
from aiohttp_session.redis_storage import RedisStorage
from aiohttp_sse import sse_response
import aioredis
from datetime import datetime
import jinja2
import json
import secrets

jenv = jinja2.Environment(loader=jinja2.PackageLoader('mychat', 'templates'))


def auth_required(handler):

    @functools.wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        sess = await get_session(request)
        user_id = sess.get('user_id')
        if user_id is None:
            return web.json_response(status=401, data={'status': 'unauthorized'})
        return await handler(request, user_id)

    return wrapped


async def index(request: web.Request) -> web.Response:
    tpl = jenv.get_template('index.html')
    sess = await get_session(request)
    user_id = sess.get('user_id')
    if user_id is None:
        user_id = f'user-{secrets.token_hex(8)}'
        sess['user_id'] = user_id
    content = tpl.render({
        'impl_type': request.app['impl_type'],
        'user_id': user_id,
    })
    return web.Response(status=200, body=content, content_type='text/html')


@auth_required
async def chat_send(request: web.Request, user_id: str) -> web.Response:
    payload = await request.json()
    chat_record = json.dumps({
        'user': user_id,
        'time': datetime.utcnow().isoformat(),
        'text': payload['text'],
    })
    await request.app['redis'].publish('chat', chat_record)
    return web.json_response(status=200, data={'status': 'ok'})


@auth_required
async def chat_subscribe(request: web.Request, user_id: str) -> web.Response:
    request_id = f'ssereq-{secrets.token_hex(8)}'
    my_queue = asyncio.Queue()
    request.app['client_queues'][request_id] = my_queue
    print(f'subscriber {user_id}:{request_id} started')
    try:
        async with sse_response(request) as response:
            while True:
                chat_record = await my_queue.get()
                print('sse.recv', chat_record)
                if chat_record is None:
                    break
                await response.send(json.dumps(chat_record))
        return response
    except asyncio.CancelledError:
        raise  # let aiohttp know this handler is cancelled
    except Exception:
        traceback.print_exc()
    finally:
        print(f'subscriber {user_id}:{request_id} terminated')
        del request.app['client_queues'][request_id]


@auth_required
async def chat_websocket(request: web.Request, user_id: str) -> web.Response:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request_id = f'wsreq-{secrets.token_hex(8)}'
    my_queue = asyncio.Queue()
    request.app['client_queues'][request_id] = my_queue

    async def chat_recv():
        try:
            while True:
                chat_record = await my_queue.get()
                print('ws.recv', chat_record)
                if chat_record is None:
                    break
                await ws.send_json(chat_record)
        except asyncio.CancelledError:
            pass

    recv_task = asyncio.create_task(chat_recv())
    print(f'subscriber {user_id}:{request_id} started')
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                chat_record = json.dumps({
                    'user': user_id,
                    'time': datetime.utcnow().isoformat(),
                    'text': payload['text'],
                })
                await request.app['redis'].publish('chat', chat_record)
        return ws
    except asyncio.CancelledError:
        recv_task.cancel()
        await recv_task
        raise
    finally:
        print(f'subscriber {user_id}:{request_id} terminated')
        del request.app['client_queues'][request_id]


async def chat_distribute(app: web.Application) -> None:
    # create a separate connection dedicated to the subscriber channel
    print('distributer started')
    redis = await aioredis.create_redis(app['redis_addr'], db=0)
    try:
        channels = await redis.subscribe('chat')
        assert len(channels) == 1
        channel = channels[0]
        async for chat_record in channel.iter():
            chat_record = json.loads(chat_record.decode('utf8'))
            for q in app['client_queues'].values():
                q.put_nowait(chat_record)
    except asyncio.CancelledError:
        pass  # we know what we are doing
    except Exception:
        traceback.print_exc()
    finally:
        # Logically, we need to "unsubscribe" the channel here,
        # but the "redis" connection is already kind-of corrupted
        # due to cancellation.
        # Just terminate our coroutine and let the Redis server
        # to recognize connection close as the signal of unsubscribe.
        print('distributer terminated')


async def app_init(app):
    app['client_queues'] = {}
    app['redis_addr'] = ('localhost', 6379)
    app['redis'] = await aioredis.create_redis_pool(app['redis_addr'], db=0)
    sess_storage = RedisStorage(
        await aioredis.create_redis_pool(app['redis_addr'], db=1),
        max_age=3600,
    )
    setup_session(app, sess_storage)
    app['distributer'] = asyncio.create_task(chat_distribute(app))


async def app_shutdown(app):
    client_queues = [*app['client_queues'].values()]  # copy for safe iteration
    for q in client_queues:
        q.put_nowait(None)
    app['distributer'].cancel()
    await app['distributer']
    app['redis'].close()
    await app['redis'].wait_closed()


@click.command()
@click.option('-h', '--host', default='127.0.0.1')
@click.option('-p', '--port', default=8080)
@click.option('-t', '--impl-type', type=click.Choice(['sse', 'websocket']), default='sse')
def main(host, port, impl_type):
    app = web.Application()
    app['impl_type'] = impl_type
    app.add_routes([
        web.get("/", index),
        web.get("/chat", chat_subscribe),
        web.get("/chat-ws", chat_websocket),
        web.post("/chat", chat_send),
    ])
    app.on_startup.append(app_init)
    app.on_shutdown.append(app_shutdown)
    web.run_app(app, host=host, port=port)


if __name__ == '__main__':
    main()
