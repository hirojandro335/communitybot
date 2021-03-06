# -*- coding: utf-8 -*-
import argparse
import json
import logging
import time
from datetime import datetime, date
import re
from os import makedirs
from os.path import expanduser, exists

import dataset
from steem import Steem
from steem.post import Post
from steembase.exceptions import PostDoesNotExist
from steem.commit import Commit

logger = logging.getLogger('communitybot')
logger.setLevel(logging.INFO)
logging.basicConfig()

CONFIG_PATH = expanduser('~/.communitybot')
STATE = expanduser("%s/state" % CONFIG_PATH)
CHECKPOINT = expanduser("%s/checkpoint" % CONFIG_PATH)


def load_state(fallback_data=None):
    try:
        return json.loads(open(STATE).read())
    except FileNotFoundError as e:
        if not exists(CONFIG_PATH):
            makedirs(CONFIG_PATH)

        dump_state(fallback_data)
        return load_state()


def dump_state(data):
    f = open(STATE, 'w+')
    f.write(json.dumps(data))
    f.close()


def load_checkpoint(fallback_block_num=None):
    try:
        return int(open(CHECKPOINT).read())
    except FileNotFoundError as e:
        if not exists(CONFIG_PATH):
            makedirs(CONFIG_PATH)

        dump_checkpoint(fallback_block_num)
        return load_checkpoint()


def dump_checkpoint(block_num):
    f = open(CHECKPOINT, 'w+')
    f.write(str(block_num))
    f.close()


class TransactionListener(object):

    def __init__(self, steem, config):
        self.steem = steem
        self.account = config["account"]
        self.mysql_uri = config["mysql_uri"]
        self.config = config
        self.commit = Commit(steem)

    def get_table(self, table):
        db = dataset.connect(self.mysql_uri)
        return db[table]

    @property
    def properties(self):
        props = self.steem.get_dynamic_global_properties()
        if not props:
            logger.info('Couldnt get block num. Retrying.')
            return self.properties
        return props

    @property
    def last_block_num(self):
        return self.properties['head_block_number']

    @property
    def block_interval(self):
        config = self.steem.get_config()
        return config["STEEMIT_BLOCK_INTERVAL"]

    def process_block(self, block_num, retry_count=0):
        block_data = self.steem.get_block(block_num)

        if not block_data:
            if retry_count > 3:
                logger.error(
                    'Retried 3 times to get this block: %s Skipping.',
                    block_num
                )
                return

            logger.error(
                'Couldnt read the block: %s. Retrying.', block_num)
            self.process_block(block_num, retry_count=retry_count + 1)

        logger.info('Processing block: %s', block_num)
        if 'transactions' not in block_data:
            return

        self.check_block(block_num)
        dump_state(self.properties)

    def run(self, start_from=None):
        if start_from is None:
            last_block = load_checkpoint(
                fallback_block_num=self.last_block_num,
            )
            logger.info('Last processed block: %s', last_block)
        else:
            last_block = start_from
        while True:

            while (self.last_block_num - last_block) > 0:
                last_block += 1
                self.process_block(last_block)
                dump_checkpoint(last_block)

            # Sleep for one block
            block_interval = self.block_interval
            logger.info('Sleeping for %s seconds.', block_interval)
            time.sleep(block_interval)

    def daily_message(self):
        post_list = []
        query = {"limit": 15, "tag": "tr"}  # limit for 5 posts
        for p in self.steem.get_discussions_by_hot(query):
            metadata = json.loads(p["json_metadata"])
            if metadata and 'utopian-io' in metadata["tags"]:
                continue
            if metadata and 'sndbox' in metadata["tags"]:
                continue

            if p["author"] == "turbot":
                continue

            link = "https://steemit.com/@%s/%s" % (p["author"], p["permlink"])
            author_link = "https://steemit.com/%s" % p["author"]
            post = Post(link, steemd_instance=self.steem)
            try:
                self.upvote(post, 20)
                time.sleep(4)
                pass
            except Exception as error:
                logger.error(error)

            post_list.append(
                "- [%s](%s) - [@%s](%s)" % (
                    p["title"], link, p["author"], author_link)
            )

        body = open(self.config["daily_message"]).read()
        body = body.replace("$post_list", "\n".join(post_list))

        today = date.today().strftime("%Y.%m.%d")
        self.steem.commit.post(
            "Son 24 saatte turbot tarafından oylanan yazılar (%s)" % today,
            body,
            "turbot",
            tags=["tr",  "turbot"]
        )

    def upvote(self, post, weight=+5):

        full_link = "@%s/%s" % (post["author"], post["permlink"])
        already_upvoted = self.get_table('upvote').find_one(
                author=post["author"], permlink=post["permlink"]
        )
        if already_upvoted:
            logger.info('Already voted. Skipping. %s', full_link)
            return

        resp = post.commit.vote(post.identifier, weight, account=self.account)
        if not resp:
            logger.error("Failed upvoting. %s", full_link)

    def handle_command(self, post):
        if post["author"] in self.config["blacklisted_users"]:
            logger.info(
                "User on blacklist. (%s). Skipping", post["permlink"])
            return

        # welcome command
        if re.findall("@%s\s!(welcome)" % self.account, post["body"]):

            main_post = Post(post.root_identifier, steemd_instance=self.steem)
            already_welcomed = self.get_table('welcome').find_one(
                author=main_post["author"]
            )

            if already_welcomed:
                logger.info(
                    "This user: %s already welcomed. Skipping" %
                    main_post["author"])
                return

            body = open(self.config["welcome_message"]).read()
            body = body.replace("$username", main_post["author"])
            main_post.reply(
                body=body,
                author=self.account,
            )
            if not main_post.is_main_post():
                logger.info("Skipping. Not a main post.")
                return
            try:
                self.upvote(main_post)
            except Exception as e:
                logger.error(e)

            logger.info("Replied and upvoted user: %s", main_post["author"])
            self.get_table('welcome').insert(dict(
                author=main_post["author"],
                permlink=main_post["permlink"],
                created_at=str(datetime.now()),
            ))
            if self.config["send_welcome_gift"] == "yes":
                self.commit.transfer(
                    main_post["author"],
                    self.config["welcome_gift"],
                    memo=self.config["welcome_gift_message"],
                    asset="SBD",
                    account=self.account
                )
        # handle help commands
        help_commands = [
            "creating_new_accounts", "bots", "curation_rewards",
            "downvote", "esteem", "security", "voting_power",
            "upvote", "tag_spam", "comment_spam", "wallet",
            "plagiarism", "posting"
        ]
        for command in help_commands:
            if re.findall(
                    "@%s\s!(%s)" % (self.account, command), post["body"]):

                message_path = "%s%s.md" % (
                    self.config["help_commands_path"],
                    command
                )
                main_post = Post(post.root_identifier, steemd_instance=self.steem)
                body = open(message_path).read()
                body = body.replace("$username", main_post["author"])
                if not main_post.is_main_post():
                    logger.info("Skipping. Not a main post.")
                    return
                main_post.reply(
                    body=body,
                    author=self.account,
                )
                logger.info("Posted %s command reply." % command)

    def check_block(self, block_num):
        operation_data = self.steem.get_ops_in_block(
            block_num, virtual_only=False)

        for operation in operation_data:
            operation_type, raw_data = operation["op"][0:2]
            if operation_type == "comment":
                try:
                    post = Post(raw_data, steemd_instance=self.steem)
                except Exception as error:
                    logger.error(error)
                    continue
                if post.is_main_post():
                    # we're only interested in comments.
                    continue
                if "@" + self.account in post["body"]:
                    try:
                        self.handle_command(post)
                    except Exception as e:
                        logger.error(e)


def listen(config, daily_message):
    logger.info('Starting TX listener...')
    steem = Steem(nodes=config.get("nodes"), keys=config["keys"])
    tx_listener = TransactionListener(steem, config)
    if daily_message == '1':
        tx_listener.daily_message()
        return
    tx_listener.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Config file in JSON format")
    parser.add_argument("daily_message", help="Post the daily message")
    args = parser.parse_args()
    config = json.loads(open(args.config).read())
    return listen(config, args.daily_message)


if __name__ == '__main__':
    main()
