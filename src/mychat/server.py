import asyncio
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


async def index(request: web.Request) -> web.Response:
    tpl = jenv.get_template('index.html')
    sess = await get_session(request)
    user_id = sess.get('user_id')
    if user_id is None:
        user_id = f'user-{secrets.token_hex(8)}'
        sess['user_id'] = user_id
    content = tpl.render({
        'user_id': user_id,
    })
    return web.Response(status=200, body=content, content_type='text/html')


async def chat_send(request: web.Request) -> web.Response:
    sess = await get_session(request)
    user_id = sess.get('user_id')
    if user_id is None:
        return web.json_response(status=401, data={'status': 'unauthorized'})
    payload = await request.json()
    chat_record = json.dumps({
        'user': user_id,
        'time': datetime.utcnow().isoformat(),
        'text': payload['text'],
    })
    await request.app['redis'].publish('chat', chat_record)
    return web.json_response(status=200, data={'status': 'ok'})


async def chat_subscribe(request: web.Request) -> web.Response:
    sess = await get_session(request)
    user_id = sess.get('user_id')
    if user_id is None:
        return web.json_response(status=401, data={'status': 'unauthorized'})
    my_queue = asyncio.Queue()
    request_id = f'req-{secrets.token_hex(8)}'
    app['subscribers'][request_id] = asyncio.current_task()
    print(f'subscriber {user_id}:{request_id} started, tid={asyncio.current_task()}')
    try:
        channels = await app['redis'].subscribe('chat')
        assert len(channels) == 1
        channel = channels[0]
        async with sse_response(request) as response:
            async for chat_record in channel.iter():
                chat_record = chat_record.decode('utf8')
                print(f'subscriber {user_id}:{request_id} recv', chat_record)
                if chat_record is None:
                    break
                await response.send(chat_record)
        return response
    except asyncio.CancelledError:
        raise
    except Exception as e:
        traceback.print_exc()
    finally:
        print(f'subscriber {user_id}:{request_id} terminated')
        del app['subscribers'][request_id]


async def app_init(app):
    app['subscribers'] = {}
    app['redis_addr'] = ('localhost', 6379)
    app['redis'] = await aioredis.create_redis_pool(app['redis_addr'], db=0)
    sess_storage = RedisStorage(
        await aioredis.create_redis_pool(app['redis_addr'], db=1),
        max_age=3600,
    )
    setup_session(app, sess_storage)


async def app_shutdown(app):
    subscribers = [*app['subscribers'].values()]  # copy for safe iteration
    for subscriber in subscribers:
        subscriber.cancel()
    await asyncio.gather(*subscribers)
    app['redis'].close()
    await app['redis'].wait_closed()


if __name__ == '__main__':
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/chat", chat_subscribe),
        web.post("/chat", chat_send),
    ])
    app.on_startup.append(app_init)
    app.on_shutdown.append(app_shutdown)
    web.run_app(app, host='127.0.0.1', port=8080)
