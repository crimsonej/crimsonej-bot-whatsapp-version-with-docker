import os

import bot

bind = "0.0.0.0:" + (os.environ.get("PORT") or os.environ.get("BOT_PORT") or str(bot.cfg("port") or 5000))
workers = 1
threads = 4
timeout = 120
preload_app = False


def post_worker_init(worker):
    bot.start_background_services()