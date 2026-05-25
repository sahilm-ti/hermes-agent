#!/usr/bin/env python3
"""Hermes Agent Release Script

Generates changelogs and creates GitHub releases with CalVer tags.

Usage:
    # Preview changelog (dry run)
    python scripts/release.py

    # Preview with semver bump
    python scripts/release.py --bump minor

    # Create the release
    python scripts/release.py --bump minor --publish

    # First release (no previous tag)
    python scripts/release.py --bump minor --publish --first-release

    # Override CalVer date (e.g. for a belated release)
    python scripts/release.py --bump minor --publish --date 2026.3.15
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "hermes_cli" / "__init__.py"
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"

# ACP Registry manifest must stay version-locked with pyproject.toml.
# tests/acp/test_registry_manifest.py enforces this lockstep so the release
# bump touches both files atomically.
ACP_REGISTRY_MANIFEST = REPO_ROOT / "acp_registry" / "agent.json"

# ──────────────────────────────────────────────────────────────────────
# Git email → GitHub username mapping
# ──────────────────────────────────────────────────────────────────────

# Auto-extracted from noreply emails + manual overrides
AUTHOR_MAP=***
    # teknium (multiple emails)
    "teknium1@gmail.com": "teknium1",
    "kenyon1977@gmail.com": "kenyonxu",
    "cipherframe@users.noreply.github.com": "CipherFrame",
    "me@promplate.dev": "CNSeniorious000",
    "yichengqiao21@gmail.com": "YarrowQiao",
    "erhanyasarx@gmail.com": "erhnysr",
    "30366221+WorldWriter@users.noreply.github.com": "WorldWriter",
    "dafeng@DafengdeMacBook-Pro.local": "WorldWriter",
    "schepers.zander1@gmail.com": "Strontvod",
    "anadi.jaggia@gmail.com": "Jaggia",
    "32201324+simpolism@users.noreply.github.com": "simpolism",
    "simpolism@gmail.com": "simpolism",
    "jake@nousresearch.com": "simpolism",
    "mgongzai@gmail.com": "vKongv",
    "0x.badfriend@gmail.com": "discodirector",
    "altriatree@gmail.com": "TruaShamu",
    "contact-me@stark-x.cn": "Stark-X",
    "nat@nthrow.io": "nthrow",
    "m@mobrienv.dev": "mikeyobrien",
    "saeed919@pm.me": "falasi",
    "chrisdlc119@outlook.com": "chdlc",
    "omar@techdeveloper.site": "nycomar",
    "qiyin.zuo@pcitc.com": "qiyin-code",
    "mr.aashiz@gmail.com": "aashizpoudel",
    "70629228+shaun0927@users.noreply.github.com": "shaun0927",
    "soju06@users.noreply.github.com": "Soju06",
    "34199905+Soju06@users.noreply.github.com": "Soju06",
    "sahil@trilogy.com": "sahilm-ti",
    "98262967+Bihruze@users.noreply.github.com": "Bihruze",
    "189280367+Lempkey@users.noreply.github.com": "Lempkey",
    "34853915+m0n3r0@users.noreply.github.com": "m0n3r0",
    "leeseoki@makestar.com": "leeseoki0",
    "kronexoi13@gmail.com": "kronexoi",
    "hua.zhong@kingsmith.com": "vgocoder",
    "leovillalbajr@gmail.com": "Lempkey",
    "nidhi2894@gmail.com": "nidhi-singh02",
    "30312689+aashizpoudel@users.noreply.github.com": "aashizpoudel",
    "oleksii.lisikh@gmail.com": "olisikh",
    "jithendranaidunara@gmail.com": "JithendraNara",
    "jeremy@geocaching.com": "outdoorsea",
    "54763683+thedavidmurray@users.noreply.github.com": "thedavidmurray",
    "leone.parise@gmail.com": "leoneparise",
    "mr@shu.io": "mrshu",
    "adam.manning@gmail.com": "am423",
    "buraysandro9@gmail.com": "ygd58",
    "108427749+buntingszn@users.noreply.github.com": "buntingszn",
    "yanglongwei06@gmail.com": "Alex-yang00",
    "teknium@nousresearch.com": "teknium1",
    "markuscontasul@gmail.com": "Glucksberg",
    "80581902+Glucksberg@users.noreply.github.com": "Glucksberg",
    "piyushvp1@gmail.com": "thelumiereguy",
    "pnascimento9596@gmail.com": "pnascimento9596",
    "dskwelmcy@163.com": "dskwe",
    "421774554@qq.com": "wuli666",
    "twebefy@gmail.com": "tw2818",
    "harish.kukreja@gmail.com": "counterposition",
    "korkyzer@gmail.com": "Korkyzer",
    "1046611633@qq.com": "zhengyn0001",
    "1095245867@qq.com": "littlewwwhite",
    "db@project-aeon.com": "db-aeon",
    "ahmed@abadr.net": "ahmedbadr3",
    "63822243+CoinTheHat@users.noreply.github.com": "CoinTheHat",
    "cleo@edaphic.xyz": "curiouscleo",
    "hirokazu.ogawa@kwansei.ac.jp": "hrkzogw",
    "datapod.k@gmail.com": "dandacompany",
    "treydong.zh@gmail.com": "TreyDong",
    "phil.thomas@gametime.co": "explainanalyze",
    "kyanam.preetham@gmail.com": "pkyanam",
    "zhizhong.xu@shopee.com": "1000Delta",
    "30397170+1000Delta@users.noreply.github.com": "1000Delta",
    "szymonclawd@mac.home": "szymonclawd",
    "257759490+szymonclawd@users.noreply.github.com": "szymonclawd",
    "101180447+worlldz@users.noreply.github.com": "worlldz",
    "zhanganzhe@tenclass.com": "luoyuctl",
    "51604064+luoyuctl@users.noreply.github.com": "luoyuctl",
    "127238744+teknium1@users.noreply.github.com": "teknium1",
    "tolle.lege+github@gmail.com": "InB4DevOps",
    "73686890+InB4DevOps@users.noreply.github.com": "InB4DevOps",
    "147827411+EloquentBrush@users.noreply.github.com": "AhmetArif0",
    "97489706+purzbeats@users.noreply.github.com": "purzbeats",
    "hugosequier@gmail.com": "Hugo-SEQUIER",
    "kylejeong21@gmail.com": "Kylejeong2",
    "128259593+Gutslabs@users.noreply.github.com": "Gutslabs",
    "50326054+nocturnum91@users.noreply.github.com": "nocturnum91",
    "52470719+gianfrancopiana@users.noreply.github.com": "gianfrancopiana",
    "223003280+Abd0r@users.noreply.github.com": "Abd0r",
    "HuangYuChuh@users.noreply.github.com": "HuangYuChuh",
    "aaronwong1989@gmail.com": "hrygo",
    "26729613+hrygo@users.noreply.github.com": "hrygo",
    "erenkar950@gmail.com": "eren-karakus0",
    "aubrey@freeman-wisco.com": "Freeman-Consulting",
    "don.rhm@gmail.com": "rahimsais",
    "40222899+rahimsais@users.noreply.github.com": "rahimsais",
    "alfred@Alfreds-Mac-mini.local": "NivOO5",
    "231191380+NivOO5@users.noreply.github.com": "NivOO5",
    "jameshuang@gmail.com": "kjames2001",
    "62420081+kjames2001@users.noreply.github.com": "kjames2001",
    "132184373+wilsen0@users.noreply.github.com": "wilsen0",
    "ra2157218@gmail.com": "Abd0r",
    "oswaldb22@users.noreply.github.com": "oswaldb22",
    "abdielv@proton.me": "AJV20",
    "mason@growagainorchids.com": "masonjames",
    "108541149+amethystani@users.noreply.github.com": "amethystani",
    "ytchen0719@gmail.com": "liquidchen",
    "am@studio1.tailb672fe.ts.net": "subtract0",
    "mike@grossmann.at": "ReqX",
    "axmaiqiu@gmail.com": "qWaitCrypto",
    "44045911+kidonng@users.noreply.github.com": "kidonng",
    "daniellsmarta@gmail.com": "DanielLSM",
    "264291321+v1b3coder@users.noreply.github.com": "v1b3coder",
    "silverchris@foxmail.com": "ming1523",
    "maksesipov@gmail.com": "Qwinty",
    "denisamania@gmail.com": "CalmProton",
    "308068+mbac@users.noreply.github.com": "mbac",
    "nicoechaniz@altermundi.net": "nicoechaniz",
    "ninso112@proton.me": "Ninso112",
    "wesleysimplicio@live.com": "wesleysimplicio",
    "matthew.dean.cater@gmail.com": "SiliconID",
    "xieniu@proton.me": "xieNniu",
    "rw8143a@american.edu": "wali-reheman",
    "egitimviscara@gmail.com": "uzunkuyruk",
    "zhekinmaksim@gmail.com": "Zhekinmaksim",
    "obafemiferanmi1999@gmail.com": "KvnGz",
    "159539633+MottledShadow@users.noreply.github.com": "MottledShadow",
    "aludwin+gh@gmail.com": "adamludwin",
    "ngusev@astralinux.ru": "NikolayGusev-astra",
    "liuguangyong201@hellobike.com": "liuguangyong93",
    "2093036+exiao@users.noreply.github.com": "exiao",
    "20nik.nosov21@gmail.com": "nik1t7n",
    "thunderggnn@gmail.com": "ggnnggez",
    "haozhe4547@gmail.com": "ehz0ah",
    "eloklam2002@gmail.com": "eloklam",
    "kevyan1998@gmail.com": "kyan12",
    "rylen.anil@gmail.com": "rylena",
    "godnanijatin@gmail.com": "jatingodnani",
    "252811164+adybag14-cyber@users.noreply.github.com": "adybag14-cyber",
    "14046872+tmimmanuel@users.noreply.github.com": "tmimmanuel",
    "112875006+donramon77@users.noreply.github.com": "donramon77",
    "657290301@qq.com": "IMHaoyan",
    "revar@users.noreply.github.com": "revaraver",
    "dengtaoyuan@dengtaoyuandeMac-mini.local": "dengtaoyuan450-a11y",
    "ysfalweshcan@gmail.com": "Junass1",
    "bartokmagic@proton.me": "Bartok9",
    "bartok9@users.noreply.github.com": "Bartok9",
    "erhanyasarx@gmail.com": "erhnysr",  # PR #25198 salvage (tool-progress flood-control)
    "cryptobyz.airdrop@gmail.com": "CryptoByz",  # PR #25630 salvage (polling conflict Stage 1+2)
    "fabioxxx@gmail.com": "fabiosiqueira",  # PR #27212 salvage (bg-process notif anchor)
    "lordfalcon.exe@gmail.com": "falconexe",  # PR #24511 salvage (sticky-IP reset)
    "fonhal@gmail.com": "fonhal",  # PR #27865/#27861 salvage (mention entities / typing fallback)
    "zyrixtrex@gmail.com": "Zyrixtrex",  # PR #26754 salvage (avoid duplicate text after auto-TTS)
    "264138787+nftpoetrist@users.noreply.github.com": "nftpoetrist",  # PR #25856 salvage (escape slash-confirm preview)
    "197455947+samahn0601@users.noreply.github.com": "samahn0601",  # PR #27887 salvage (retry wrapped connect timeouts)
    "gonzes7@gmail.com": "aqilaziz",  # PR #26406 salvage (preserve native audio outside Telegram)
    "karthikeyann@users.noreply.github.com": "karthikeyann",  # PR #26609 salvage (DM-topic routing pin)
    "rino.alpin@gmail.com": "kunci115",  # PR #27098 salvage (thread-not-found retry)
    "hayka-pacha@users.noreply.github.com": "hayka-pacha",  # PR #25270 salvage (registry-aware mcp_ prefix strip)
    "237601532+chromalinx@users.noreply.github.com": "chromalinx",  # PR #27014 salvage (commands for groups+DM)
    "booker1207@gmail.com": "booker1207",  # PR #25132 salvage (gate profile bots by allowed topics)
    "kiranvk2011@gmail.com": "kiranvk-2011",  # PR #24815 salvage (image documents → vision)
    "kosmonaut-t@centrum.cz": "rak135",  # PR #25960 salvage (Windows /restart)
    "bot.chi.online@gmail.com": "B0Tch1",  # PR #27634 salvage (disable_topic_auto_rename)
    "1037461232@qq.com": "jackjin1997",  # PR #27239 salvage (restore DM topic thread_id after split)
    "soynchuux@gmail.com": "soynchux",  # PR #27806 salvage (chat-scoped auth without user_id)
    "psikonetik@gmail.com": "el-analista",  # PR #25368 salvage (cron topic fallback report)
    "75435655+khungate@users.noreply.github.com": "khungate",  # PR #25829 salvage (gmail-triage gt: callbacks)
    "stevehq26-bot@users.noreply.github.com": "stevehq26-bot",  # PR #28015 salvage (quick-command-only menus)
    "seaverb@icloud.com": "brndnsvr",  # PR #25327 salvage (channel post updates)
    "oracle@jarviss-mbp.home": "houenyang-momo",  # PR #24014 salvage (quiet noisy errors)
    "57119977+OCWC22@users.noreply.github.com": "OCWC22",  # PR #24581 salvage (multi-bot exclusive mentions)
    "ai-hana-ai@users.noreply.github.com": "ai-hana-ai",  # PR #23928 salvage (ignore_root_dm)
    "mx.indigo.karasu@gmail.com": "indigokarasu",  # PR #26636 salvage (pin user message)
    "516972+alber70g@users.noreply.github.com": "alber70g",  # PR #25280 salvage (skip-STT + 2GB cap)
    "282919977+eliteworkstation94-ai@users.noreply.github.com": "eliteworkstation94-ai",  # PR #28157 salvage (group reply session splits)
    "androidhtml@yandex.com": "hllqkb",
    "25840394+Bongulielmi@users.noreply.github.com": "Bongulielmi",
    "jonathan.troyer@overmatch.com": "JTroyerOvermatch",
    "harryykyle1@gmail.com": "hharry11",
    "wysie@users.noreply.github.com": "wysie",
    "jkausel@gmail.com": "jkausel-ai",
    "e.silacandmr@gmail.com": "Es1la",
    "51599529+stephen0110@users.noreply.github.com": "stephen0110",
    "265632032+sonic-netizen@users.noreply.github.com": "sonic-netizen",
    "82531659+mwnickerson@users.noreply.github.com": "mwnickerson",
    "sandrohub013@gmail.com": "SandroHub013",
    "maciekczech@users.noreply.github.com": "maciekczech",
    "154585401+LeonSGP43@users.noreply.github.com": "LeonSGP43",
    "cine.dreamer.one@gmail.com": "LeonSGP43",
    "zjtan1@gmail.com": "zeejaytan",
    "asslaenn5@gmail.com": "Aslaaen",
    "trae.anderson17@icloud.com": "Tkander1715",
    "beardthelion@users.noreply.github.com": "beardthelion",
    "tangyuanjc@JCdeAIfenshendeMac-mini.local": "tangyuanjc",
    "leon@agentlinker.ai": "agentlinker",
    "santoshhumagain1887@gmail.com": "npmisantosh",
    "39641663+luarss@users.noreply.github.com": "luarss",
    "16263913+zccyman@users.noreply.github.com": "zccyman",
    "zccyman@users.noreply.github.com": "zccyman",  # PR #26998 (auxiliary fallback chain)
    "ahmetosrak@Ahmet-MacBook-Air.local": "Osraka",
    "98612432+Osraka@users.noreply.github.com": "Osraka",
    "112634774+ryptotalent@users.noreply.github.com": "ryptotalent",
    "270097726+hookinglau@users.noreply.github.com": "hookinglau",
    "5029547+AllynSheep@users.noreply.github.com": "AllynSheep",
    "allyn0306@gmail.com": "AllynSheep",
    "46887634+aqilaziz@users.noreply.github.com": "aqilaziz",
    "gonzes7@gmail.com": "aqilaziz",
    "6966326+laoli-no1@users.noreply.github.com": "laoli-no1",
    "laoli_no1@163.com": "laoli-no1",
    "39730900+NorethSea@users.noreply.github.com": "NorethSea",
    "963979204@qq.com": "NorethSea",
    "2283389+JamesX88@users.noreply.github.com": "JamesX88",
    "JamesX88@users.noreply.github.com": "JamesX88",
    "novax635@gmail.com": "novax635",
    "krionex1@gmail.com": "Krionex",
    "rxdxxxx@users.noreply.github.com": "rxdxxxx",
    "ma.haohao2@xydigit.com": "MaHaoHao-ch",
    "29756950+revaraver@users.noreply.github.com": "revaraver",
    "nexus@eptic.me": "TheEpTic",
    "74554762+wmagev@users.noreply.github.com": "wmagev",
    "ashermorse@icloud.com": "ashermorse",
    "happy5318@users.noreply.github.com": "happy5318",
    "anatoliygranichenko@gmail.com": "wabrent",
    "cash.williams@acquia.com": "CashWilliams",
    "chengoak@users.noreply.github.com": "chengoak",
    "mrhanoi@outlook.com": "qxxaa",
    "guillaume.meyer@outlook.com": "guillaumemeyer",
    "emelyanenko.kirill@gmail.com": "EmelyanenkoK",
    "lazycat.manatee@gmail.com": "manateelazycat",
    "bzarnitz13@gmail.com": "Beandon13",
    "tony@tonysimons.dev": "asimons81",
    "jetha@google.com": "jethac",
    "jani@0xhoneyjar.xyz": "deep-name",
    # LINE messaging plugin (synthesis PR)
    "32443648+leepoweii@users.noreply.github.com": "leepoweii",
    "openclaw@liyangchen.me": "liyoungc",
    "charles@perng.com": "perng",
    "soichiro0111.dev@gmail.com": "soichiyo",
    "0xde@pieverse.io": "David-0x221Eight",
    "77736378+David-0x221Eight@users.noreply.github.com": "David-0x221Eight",
    "74749461+yuga-hashimoto@users.noreply.github.com": "yuga-hashimoto",
    "xiangyong@zspace.cn": "CES4751",
    "harish.kukreja@gmail.com": "counterposition",
    "nidhi2894@gmail.com": "nidhi-singh02",
    "35294173+Fearvox@users.noreply.github.com": "Fearvox",
    "hypnus.yuan@gmail.com": "Hypnus-Yuan",
    "15558128926@qq.com": "xsfX20",
    "binhnt.ht.92@gmail.com": "binhnt92",
    "johnny@Jons-MBA-M4.local": "acesjohnny",
    "1581133593@qq.com": "liu-collab",
    "haidaoe@proton.me": "haidao1919",
    "50561768+zhanggttry@users.noreply.github.com": "zhanggttry",
    "formulahendry@gmail.com": "formulahendry",
    "93757150+bogerman1@users.noreply.github.com": "bogerman1",
    "132852777+rob-maron@users.noreply.github.com": "rob-maron",
    # Matrix parity salvage batch (April 2026)
    "sr@samirusani": "samrusani",
    "angelclaw@AngelMacBook.local": "angel12",
    "charles@cryptoassetrecovery.com": "charles-brooks",
    # DeepSeek v4 + Kimi thinking-mode reasoning_content salvage (April 2026)
    "luwinyang@deepseek.com": "lsdsjy",
    "season.saw@gmail.com": "season179",
    "heathley@Heathley-MacBook-Air.local": "heathley",
    "maliyldzhn@gmail.com": "heathley",
    "vlad19@gmail.com": "dandaka",
    "adamrummer@gmail.com": "cyclingwithelephants",
    # Temporary tool-progress cleanup salvage (May 2026)
    "Mrcharlesiv@gmail.com": "mrcharlesiv",
    "nbot@liizfq.top": "liizfq",
    "274096618+hermes-agent-dhabibi@users.noreply.github.com": "dhabibi",
    "dejie.guo@gmail.com": "JayGwod",
    "133716830+0xKingBack@users.noreply.github.com": "0xKingBack",
    "daixin1204@gmail.com": "SimbaKingjoe",
    "maxence@groine.fr": "MaxyMoos",
    "61830395+leprincep35700@users.noreply.github.com": "leprincep35700",
    # OpenViking viking_read salvage (April 2026)
    "hitesh@gmail.com": "htsh",
    "pty819@outlook.com": "pty819",
    "pty819@users.noreply.github.com": "pty819",
    "14341805+pty819@users.noreply.github.com": "pty819",
    "517024110@qq.com": "chennest",
    # Curator fixes (Apr 30 2026)
    "yuxiangl490@gmail.com": "y0shua1ee",
    "manmit0x@gmail.com": "0xDevNinja",
    "stevekelly622@gmail.com": "steezkelly",
    "brian@dralth.com": "btorresgil",
    "momowind@gmail.com": "momowind",
    "clockwork-codex@users.noreply.github.com": "misery-hl",
    "207811921+misery-hl@users.noreply.github.com": "misery-hl",
    "20nik.nosov21@gmail.com": "nik1t7n",
    "90299797+nik1t7n@users.noreply.github.com": "nik1t7n",
    "suncokret@protonmail.com": "suncokret12",
    "mio.imoto.ai@gmail.com": "mioimotoai-lgtm",
    "aamirjawaid@microsoft.com": "heyitsaamir",
    "johnnncenaaa77@gmail.com": "johnncenae",
    "thomasjhon6666@gmail.com": "ThomassJonax",
    "focusflow.app.help@gmail.com": "yes999zc",
    "rob@atlas.lan": "rmoen",
    # Slack ephemeral slash-ack salvage (May 2026)
    "probepark@users.noreply.github.com": "probepark",
    # Slack batch salvage (May 2026)
    "280484231+prive-fe-bot@users.noreply.github.com": "priveperfumes",
    "amr@ghanem.sa": "amroessam",
    "paperlantern.agent@gmail.com": "Hinotoi-agent",
    "valda@underscore.jp": "valda",
    "162235745+0z1-ghb@users.noreply.github.com": "0z1-ghb",
    "yes999zc@163.com": "yes999zc",
    "343873859@qq.com": "DrStrangerUJN",
    "252818347@qq.com": "hejuntt1014",
    "uzmpsk.dilekakbas@gmail.com": "dlkakbs",
    "beliefanx@gmail.com": "BeliefanX",
    "changchun989@proton.me": "changchun989",
    "jefferson@heimdallstrategy.com": "Mind-Dragon",
    "44753291+Nanako0129@users.noreply.github.com": "Nanako0129",
    "steve.westerhouse@origami-analytics.com": "westers",
    "yeyitech@users.noreply.github.com": "yeyitech",
    "260878550+beenherebefore@users.noreply.github.com": "beenherebefore",
    "79389617+txbxxx@users.noreply.github.com": "txbxxx",
    "liuhao03@bilibili.com": "liuhao1024",
    "130918800+devorun@users.noreply.github.com": "devorun",
    "surat.s@itm.kmutnb.ac.th": "beesrsj2500",
    "beesr@bee.localdomain": "beesrsj2500",
    "mind-dragon@nous.research": "Mind-Dragon",
    "juntingpublic@gmail.com": "JustinUssuri",
    "mtf201013@gmail.com": "ma-pony",
    "sonoyuncudmr@gmail.com": "Sonoyunchu",
    "43525405+yatesjalex@users.noreply.github.com": "yatesjalex",
    "maks.mir@yahoo.com": "say8hi",
    "27719690+Mirac1eSky@users.noreply.github.com": "Mirac1eSky",
    "web3blind@users.noreply.github.com": "web3blind",
    "julia@alexland.us": "alexg0bot",
    "christian@scheid.tech": "scheidti",
    # Moonshot schema anyOf+enum salvage (May 2026)
    "git@local.invalid": "hendrixfreire",
    "1060770+benjaminsehl@users.noreply.github.com": "benjaminsehl",
    "nerijusn76@gmail.com": "Nerijusas",
    # Compaction salvage batch (May 2026)
    "MacroAnarchy@users.noreply.github.com": "MacroAnarchy",
    "itonov@proton.me": "Ito-69",
    "glesstech@gmail.com": "georgeglessner",
    "maxim.smetanin@gmail.com": "maxims-oss",
    # Codex Spark restoration salvage (May 2026)
    "olegwn@gmail.com": "nederev",
    "vesper@askclaw.dev": "askclaw-vesper",
    "nazirulhafiy@gmail.com": "nazirulhafiy",
    "CREWorx@users.noreply.github.com": "BadTechBandit",
    "yoimexex@gmail.com": "Yoimex",
    "6548898+romanornr@users.noreply.github.com": "romanornr",
    "foxion37@gmail.com": "foxion37",
    "bloodcarter@gmail.com": "bloodcarter",
    "scott@scotttrinh.com": "scotttrinh",
    "quocanh261997@gmail.com": "quocanh261997",
    "savanne.kham@protonmail.com": "savanne-kham

... [OUTPUT TRUNCATED - 40698 chars omitted out of 90698 total] ...

6374 (tool_trace error detection)
    "188585318+dgians@users.noreply.github.com": "dgians",  # PR #26034 (.ts/.py/.sh docs types)
    "zealy@tz.co": "dgians",  # PR #26034 (bot-committed by zealy-tzco under dgians' PR)
    "mottei.survive@gmail.com": "flanny7",  # PR #27030 (setup_open_webui python var)
    "20530505+flanny7@users.noreply.github.com": "flanny7",
    "hermesagent26@gmail.com": "hermesagent26",  # PR #26438 (kimi model-name reasoning pad)
    "276067471+hermesagent26@users.noreply.github.com": "hermesagent26",
    "71590782+kriscolab@users.noreply.github.com": "kriscolab",  # PR #26926 (deepseek default_aux_model)
    # batch salvage (May 2026 LHF run, group 3)
    "darvsum@users.noreply.github.com": "darvsum",  # PR #26766 (preserve discover_models in normalize)
    "peter@Peters-Mac-mini.local": "hueilau",  # PR #26498 (strip image parts for non-vision)
    "33933019+hueilau@users.noreply.github.com": "hueilau",
    "32297275+Timur00Kh@users.noreply.github.com": "Timur00Kh",  # PR #27114 (telegram DM topic for synthetic events)
    "al.bellemare@gmail.com": "Grogger",  # PR #27061 (windows console flash suppress)
    "7065068+Grogger@users.noreply.github.com": "Grogger",
    "18091625+Grogger@users.noreply.github.com": "Grogger",  # stale salvage commit alias (PR #28330)
    "clement@nousresearch.com": "lemassykoi",  # PR #27042 (model-switch probe keyless providers)
    "16377344+lemassykoi@users.noreply.github.com": "lemassykoi",
    "draplater@icloud.com": "draplater",  # PR #26707 (goal judge current time)
    "6349758+draplater@users.noreply.github.com": "draplater",
    "pr7426@users.noreply.github.com": "pr7426",  # PR #27048 (cron parallel job loss)
    "rahulnilvan43@gmail.com": "therahul-yo",  # PR #26215 (mock keychain in tests)
    "kingsleyemeka117@gmail.com": "flamiinngo",  # PR #27205 (UnicodeEncodeError footgun checker)
    # batch salvage (May 2026 LHF run, group 4)
    "283442588+EloquentBrush0x@users.noreply.github.com": "EloquentBrush0x",  # PR #26657 (trust_env aiohttp)
    "205509009+subtract0@users.noreply.github.com": "subtract0",  # PR #25658 (zsh $status -> $rc)
    "patryk@jarmakowicz.me": "zwolniony",  # PR #26961 (gemini x-goog-api-key)
    "12735938+zwolniony@users.noreply.github.com": "zwolniony",
    "ambuj@dodopayments.com": "that-ambuj",  # PR #26582 (preserve underscores)
    "zccyman@163.com": "zccyman",  # PR #25294 (custom provider api_key_env alias)
    # xAI cluster batch salvage (May 2026)
    "lgndscntn@gmail.com": "Fewmanism",  # PR #27420 (threaded xAI OAuth callback)
    "slimydog@Faisals-Mac-mini.local": "Slimydog21",  # PR #28021 (strip slash enums xAI Responses)
    "194121339+Slimydog21@users.noreply.github.com": "Slimydog21",  # PR #28021 salvage (noreply form)
    "bitkyc08@gmail.com": "lidge-jun",  # PR #26814 (api server browser security headers)
    "sp_ps@Mac-mini.lan": "phoenixshen",  # PR #26768 (respect user-configured vision model)
    "1594534+phoenixshen@users.noreply.github.com": "phoenixshen",
    "147827411+AhmetArif0@users.noreply.github.com": "AhmetArif0",  # PR #26635 (line proxy env vars)
    # batch salvage (May 2026 LHF run, group 5)
    "hari@Hariharans-MacBook-Air-8.local": "haran2001",  # PR #27070 (i18n catalog test)
    "hariharan15151@gmail.com": "haran2001",  # PR #27068 (qwen3.6-plus 1M context)
    "56040092+haran2001@users.noreply.github.com": "haran2001",
    "1472110+ms-alan@users.noreply.github.com": "ms-alan",  # PR #26443 (reload-skills tab completion)
    "ganlinbupt@gmail.com": "godlin-gh",  # PR #26118 (ACP polished tools)
    "wesley.simplicio.ext@siemens-energy.com": "wesleysimplicio",  # PR #25777 (xterm.js native selection)
    "6108320+wesleysimplicio@users.noreply.github.com": "wesleysimplicio",
    "carryzuo00@gmail.com": "Carry00",  # PR #26851 (doctor SSH env vars)
    "alaamohanad169-ship-it@users.noreply.github.com": "alaamohanad169-ship-it",  # PR #26036 (telegram typing after send)
    "vigo@hermes": "hawknewton",  # PR #26294 (bedrock boto3 lazy_deps)
    "211668+hawknewton@users.noreply.github.com": "hawknewton",
    "quenvix00@gmail.com": "QuenVix",  # PR #26761/26772 salvage
    "164776164+QuenVix@users.noreply.github.com": "QuenVix",
    "262945885+Mind-Dragon@users.noreply.github.com": "Mind-Dragon",  # PR #26966 salvage
    "soynchuux@gmail.com": "soynchux",  # PR #27060 salvage
    "209694554+soynchux@users.noreply.github.com": "soynchux",
    # batch salvage (May 2026 LHF run, group 6 — final)
    "6666242+bird@users.noreply.github.com": "bird",  # PR #25219 (gateway docker exit-75 restart)
    "david@loadmagic.ai": "davidcampbelldc",  # PR #26834 (web_server proxy_headers=False)
    "165905879+davidcampbelldc@users.noreply.github.com": "davidcampbelldc",
    "hoangv.pham0803@gmail.com": "hehehe0803",  # PR #26212 salvage (codex kanban writable root)
    "26063003+hehehe0803@users.noreply.github.com": "hehehe0803",
    "38348871+vaddisrinivas@users.noreply.github.com": "vaddisrinivas",  # PR #26394 salvage (Docker messaging extra)
    # batch salvage (May 2026 LHF run, group 7)
    "198679067+02356abc@users.noreply.github.com": "02356abc",  # PR #28286 salvage (wecom CLOSING)
    "1743117+burjorjee@users.noreply.github.com": "burjorjee",  # PR #28201 salvage (inline-shell timeout guard)
    "keki@MacBookPro.attlocal.net": "burjorjee",
    "264690993+oseftg@users.noreply.github.com": "oseftg",  # PR #28168 salvage (natural ending emoji/caret)
    "hex.hermes@agentmail.to": "oseftg",
    "236912655+rudi193-cmd@users.noreply.github.com": "rudi193-cmd",  # PR #28241 salvage (empty credential pool)
    "rudi193@gmail.com": "rudi193-cmd",
    "86684667+sadiksaifi@users.noreply.github.com": "sadiksaifi",  # PR #27982 salvage (kanban horiz scroll)
    "mail@sadiksaifi.dev": "sadiksaifi",
    # batch salvage (May 2026 LHF run, group 8)
    "266824395+AceWattGit@users.noreply.github.com": "AceWattGit",  # PR #28159 salvage (_pool_may_recover NameError)
    "57024493+YuanHanzhong@users.noreply.github.com": "YuanHanzhong",  # PR #28032 salvage (x.com status link-like)
    "24368158+colin-chang@users.noreply.github.com": "colin-chang",  # PR #28245/#28249/#28251 salvage
    "zhangcheng5468@gmail.com": "colin-chang",
    "172729123+felix-windsor@users.noreply.github.com": "felix-windsor",  # PR #28019 salvage (cron asterisks)
    "felixwindsor3344@gmail.com": "felix-windsor",
    "259054917+houenyang-momo@users.noreply.github.com": "houenyang-momo",  # PR #28205 salvage (charizard contrast)
    "35931201+iqdoctor@users.noreply.github.com": "iqdoctor",  # PR #28095 salvage (windows installer docs)
    "29513231+joe102084@users.noreply.github.com": "joe102084",  # PR #28151 salvage (whitespace cron responses)
    "joe102084@gmail.com": "joe102084",
    "4139778+jvinals@users.noreply.github.com": "jvinals",  # PR #27936 salvage (Slack U-IDs)
    "3001335+maxmilian@users.noreply.github.com": "maxmilian",  # PR #28267 salvage (Change Model portal)
    "maxmilian@gmail.com": "maxmilian",
    "41468846+samggggflynn@users.noreply.github.com": "samggggflynn",  # PR #27952 salvage (dingtalk pre_start)
    "abc401011721@gmail.com": "samggggflynn",
    "yannsunn@users.noreply.github.com": "yannsunn",  # PR #28064 salvage (xai proxy upstream)
    "yannsunn1116@gmail.com": "yannsunn",
    "asdlem@users.noreply.github.com": "asdlem",  # PR #27852 salvage (clarify full text in body)
    # batch salvage (May 2026 LHF run, group 9)
    "1779909+jdelmerico@users.noreply.github.com": "jdelmerico",  # PR #28278 salvage (signal require_mention)
    "20639347+justemu@users.noreply.github.com": "justemu",  # PR #27996 salvage (matrix thread_require_mention)
    "justemu@users.noreply.github.com": "justemu",
    "57024493+YuanHanzhong@users.noreply.github.com": "YuanHanzhong",  # PR #28029 salvage (dashboard scrollback)
    "YuanHanzhong@users.noreply.github.com": "YuanHanzhong",
    "1663402+noctilust@users.noreply.github.com": "noctilust",  # PR #28080 salvage (stale TUI resume env)
    "1663402+freeurmind@users.noreply.github.com": "noctilust",
    "35164907+MoonJuhan@users.noreply.github.com": "MoonJuhan",  # PR #28288 salvage (unreadable JSONL transcripts)
    "codemike@naver.com": "MoonJuhan",
    "201563152+outsourc-e@users.noreply.github.com": "outsourc-e",  # PR #28164 salvage (cron emoji ZWJ)
    "201803425+Zyrixtrex@users.noreply.github.com": "Zyrixtrex",  # PR #28275 salvage (Google OAuth timeout)
    "zyrixtrex@gmail.com": "Zyrixtrex",
    "120500656+ooovenenoso@users.noreply.github.com": "ooovenenoso",  # PR #28256 salvage (tool loop recovery hints)
    "120500656+oooindefatigable@users.noreply.github.com": "ooovenenoso",
    "vanthinh6886@gmail.com": "vanthinh6886",  # PR #28018 salvage (yaml/flock/atomic write guards)
    "erik.engervall@gmail.com": "erikengervall",  # PR #28774 (firecrawl integration tag)
    "egilewski@egilewski.com": "egilewski",  # PR #30432 (MEDIA path traversal fix, GHSA-jmf9-9729-7pp8)
    "edison@mcclean.codes": "McClean-Edison",  # PR #29817 (register_auxiliary_task plugin API)
    "zhangsamuel12@gmail.com": "SamuelZ12",  # PR #7480 (show recap after in-session resume)
    "490408354@qq.com": "daizhonggeng",  # PR #9020 (numbered /resume selection)
}


def git(*args, cwd=None):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
        cwd=cwd or str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f"git {' '.join(args)} failed: {result.stderr}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def git_result(*args, cwd=None):
    """Run a git command and return the full CompletedProcess."""
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True,
        text=True,
        cwd=cwd or str(REPO_ROOT),
    )


def get_last_tag():
    """Get the most recent CalVer tag."""
    tags = git("tag", "--list", "v20*", "--sort=-v:refname")
    if tags:
        return tags.split("\n")[0]
    return None


def next_available_tag(base_tag: str) -> tuple[str, str]:
    """Return a tag/calver pair, suffixing same-day releases when needed."""
    if not git("tag", "--list", base_tag):
        return base_tag, base_tag.removeprefix("v")

    suffix = 2
    while git("tag", "--list", f"{base_tag}.{suffix}"):
        suffix += 1
    tag_name = f"{base_tag}.{suffix}"
    return tag_name, tag_name.removeprefix("v")


def get_current_version():
    """Read current semver from __init__.py."""
    content = VERSION_FILE.read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else "0.0.0"


def bump_version(current: str, part: str) -> str:
    """Bump a semver version string."""
    parts = current.split(".")
    if len(parts) != 3:
        parts = ["0", "0", "0"]
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"Unknown bump part: {part}")

    return f"{major}.{minor}.{patch}"


def update_version_files(semver: str, calver_date: str):
    """Update version strings in source files."""
    # Update __init__.py
    content = VERSION_FILE.read_text()
    content = re.sub(
        r'__version__\s*=\s*"[^"]+"',
        f'__version__ = "{semver}"',
        content,
    )
    content = re.sub(
        r'__release_date__\s*=\s*"[^"]+"',
        f'__release_date__ = "{calver_date}"',
        content,
    )
    VERSION_FILE.write_text(content)

    # Update pyproject.toml
    pyproject = PYPROJECT_FILE.read_text()
    pyproject = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{semver}"',
        pyproject,
        flags=re.MULTILINE,
    )
    PYPROJECT_FILE.write_text(pyproject)

    # Update ACP Registry manifest + npm launcher (must stay version-locked
    # with pyproject — enforced by tests/acp/test_registry_manifest.py).
    _update_acp_registry_versions(semver)


def _update_acp_registry_versions(semver: str) -> None:
    """Bump the ACP Registry manifest's version + uvx package pin in lockstep
    with pyproject.

    Skips silently if the manifest is missing — older release branches predate
    the ACP Registry assets.
    """
    if ACP_REGISTRY_MANIFEST.exists():
        manifest = json.loads(ACP_REGISTRY_MANIFEST.read_text(encoding="utf-8"))
        manifest["version"] = semver
        uvx = manifest.get("distribution", {}).get("uvx", {})
        if "package" in uvx:
            uvx["package"] = f"hermes-agent[acp]=={semver}"
        # Preserve trailing newline + 2-space indent the file already uses.
        ACP_REGISTRY_MANIFEST.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


def build_release_artifacts(semver: str) -> list[Path]:
    """Build sdist/wheel artifacts for the current release.

    Tries ``uv build`` first (matching the CI workflow), falls back to
    ``python -m build`` if uv is unavailable.
    """
    dist_dir = REPO_ROOT / "dist"
    shutil.rmtree(dist_dir, ignore_errors=True)

    # Prefer uv build (matches CI workflow), fall back to python -m build.
    uv_bin = shutil.which("uv")
    if uv_bin:
        cmd = [uv_bin, "build", "--sdist", "--wheel"]
    else:
        cmd = [sys.executable, "-m", "build", "--sdist", "--wheel"]

    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("  ⚠ Could not build Python release artifacts.")
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        if stderr:
            print(f"    {stderr.splitlines()[-1]}")
        elif stdout:
            print(f"    {stdout.splitlines()[-1]}")
        print("    Install uv or the 'build' package to attach sdist/wheel assets.")
        return []

    artifacts = sorted(p for p in dist_dir.iterdir() if p.is_file())
    matching = [p for p in artifacts if semver in p.name]
    if not matching:
        print("  ⚠ Built artifacts did not match the expected release version.")
        return []
    return matching


def resolve_author(name: str, email: str) -> str:
    """Resolve a git author to a GitHub @mention."""
    # Try email lookup first
    gh_user = AUTHOR_MAP.get(email)
    if gh_user:
        return f"@{gh_user}"

    # Try noreply pattern
    noreply_match = re.match(r"(\d+)\+(.+)@users\.noreply\.github\.com", email)
    if noreply_match:
        return f"@{noreply_match.group(2)}"

    # Try username@users.noreply.github.com
    noreply_match2 = re.match(r"(.+)@users\.noreply\.github\.com", email)
    if noreply_match2:
        return f"@{noreply_match2.group(1)}"

    # Fallback to git name
    return name


def categorize_commit(subject: str) -> str:
    """Categorize a commit by its conventional commit prefix."""
    subject_lower = subject.lower()

    # Match conventional commit patterns
    patterns = {
        "breaking": [r"^breaking[\s:(]", r"^!:", r"BREAKING CHANGE"],
        "features": [r"^feat[\s:(]", r"^feature[\s:(]", r"^add[\s:(]"],
        "fixes": [r"^fix[\s:(]", r"^bugfix[\s:(]", r"^bug[\s:(]", r"^hotfix[\s:(]"],
        "improvements": [r"^improve[\s:(]", r"^perf[\s:(]", r"^enhance[\s:(]",
                         r"^refactor[\s:(]", r"^cleanup[\s:(]", r"^clean[\s:(]",
                         r"^update[\s:(]", r"^optimize[\s:(]"],
        "docs": [r"^doc[\s:(]", r"^docs[\s:(]"],
        "tests": [r"^test[\s:(]", r"^tests[\s:(]"],
        "chore": [r"^chore[\s:(]", r"^ci[\s:(]", r"^build[\s:(]",
                  r"^deps[\s:(]", r"^bump[\s:(]"],
    }

    for category, regexes in patterns.items():
        for regex in regexes:
            if re.match(regex, subject_lower):
                return category

    # Heuristic fallbacks
    if any(w in subject_lower for w in ["add ", "new ", "implement", "support "]):
        return "features"
    if any(w in subject_lower for w in ["fix ", "fixed ", "resolve", "patch "]):
        return "fixes"
    if any(w in subject_lower for w in ["refactor", "cleanup", "improve", "update "]):
        return "improvements"

    return "other"


def clean_subject(subject: str) -> str:
    """Clean up a commit subject for display."""
    # Remove conventional commit prefix
    cleaned = re.sub(r"^(feat|fix|docs|chore|refactor|test|perf|ci|build|improve|add|update|cleanup|hotfix|breaking|enhance|optimize|bugfix|bug|feature|tests|deps|bump)[\s:(!]+\s*", "", subject, flags=re.IGNORECASE)
    # Remove trailing issue refs that are redundant with PR links
    cleaned = cleaned.strip()
    # Capitalize first letter
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def parse_coauthors(body: str) -> list:
    """Extract Co-authored-by trailers from a commit message body.

    Returns a list of {'name': ..., 'email': ...} dicts.
    Filters out AI assistants and bots (Claude, Copilot, Cursor, etc.).
    """
    if not body:
        return []
    # AI/bot emails to ignore in co-author trailers
    _ignored_emails = {"noreply@anthropic.com", "noreply@github.com",
                       "cursoragent@cursor.com", "hermes@nousresearch.com"}
    _ignored_names = re.compile(r"^(Claude|Copilot|Cursor Agent|GitHub Actions?|dependabot|renovate)", re.IGNORECASE)
    pattern = re.compile(r"Co-authored-by:\s*(.+?)\s*<([^>]+)>", re.IGNORECASE)
    results = []
    for m in pattern.finditer(body):
        name, email = m.group(1).strip(), m.group(2).strip()
        if email in _ignored_emails or _ignored_names.match(name):
            continue
        results.append({"name": name, "email": email})
    return results


def get_commits(since_tag=None):
    """Get commits since a tag (or all commits if None)."""
    if since_tag:
        range_spec = f"{since_tag}..HEAD"
    else:
        range_spec = "HEAD"

    # Format: hash<US>author_name<US>author_email<US>subject\0body
    # Using %x1f (unit separator) to avoid conflict with | in author names
    log = git(
        "log", range_spec,
        "--format=%H%x1f%an%x1f%ae%x1f%s%x00%b%x00",
        "--no-merges",
    )

    if not log:
        return []

    commits = []
    # Split on double-null to get each commit entry, since body ends with \0
    # and format ends with \0, each record ends with \0\0 between entries
    for entry in log.split("\0\0"):
        entry = entry.strip()
        if not entry:
            continue
        # Split on first null to separate "hash<US>name<US>email<US>subject" from "body"
        if "\0" in entry:
            header, body = entry.split("\0", 1)
            body = body.strip()
        else:
            header = entry
            body = ""
        parts = header.split("\x1f", 3)
        if len(parts) != 4:
            continue
        sha, name, email, subject = parts
        coauthor_info = parse_coauthors(body)
        coauthors = [resolve_author(ca["name"], ca["email"]) for ca in coauthor_info]
        commits.append({
            "sha": sha,
            "short_sha": sha[:8],
            "author_name": name,
            "author_email": email,
            "subject": subject,
            "category": categorize_commit(subject),
            "github_author": resolve_author(name, email),
            "coauthors": coauthors,
        })

    return commits


def get_pr_number(subject: str) -> str | None:
    """Extract PR number from commit subject if present."""
    match = re.search(r"#(\d+)", subject)
    if match:
        return match.group(1)
    return None


def generate_changelog(commits, tag_name, semver, repo_url="https://github.com/NousResearch/hermes-agent",
                       prev_tag=None, first_release=False):
    """Generate markdown changelog from categorized commits."""
    lines = []

    # Header
    now = datetime.now()
    date_str = now.strftime("%B %d, %Y")
    lines.append(f"# Hermes Agent v{semver} ({tag_name})")
    lines.append("")
    lines.append(f"**Release Date:** {date_str}")
    lines.append("")

    if first_release:
        lines.append("> 🎉 **First official release!** This marks the beginning of regular weekly releases")
        lines.append("> for Hermes Agent. See below for everything included in this initial release.")
        lines.append("")

    # Group commits by category
    categories = defaultdict(list)
    all_authors = set()
    teknium_aliases = {"@teknium1"}

    for commit in commits:
        categories[commit["category"]].append(commit)
        author = commit["github_author"]
        if author not in teknium_aliases:
            all_authors.add(author)
        for coauthor in commit.get("coauthors", []):
            if coauthor not in teknium_aliases:
                all_authors.add(coauthor)

    # Category display order and emoji
    category_order = [
        ("breaking", "⚠️ Breaking Changes"),
        ("features", "✨ Features"),
        ("improvements", "🔧 Improvements"),
        ("fixes", "🐛 Bug Fixes"),
        ("docs", "📚 Documentation"),
        ("tests", "🧪 Tests"),
        ("chore", "🏗️ Infrastructure"),
        ("other", "📦 Other Changes"),
    ]

    for cat_key, cat_title in category_order:
        cat_commits = categories.get(cat_key, [])
        if not cat_commits:
            continue

        lines.append(f"## {cat_title}")
        lines.append("")

        for commit in cat_commits:
            subject = clean_subject(commit["subject"])
            pr_num = get_pr_number(commit["subject"])
            author = commit["github_author"]

            # Build the line
            parts = [f"- {subject}"]
            if pr_num:
                parts.append(f"([#{pr_num}]({repo_url}/pull/{pr_num}))")
            else:
                parts.append(f"([`{commit['short_sha']}`]({repo_url}/commit/{commit['sha']}))")

            if author not in teknium_aliases:
                parts.append(f"— {author}")

            lines.append(" ".join(parts))

        lines.append("")

    # Contributors section
    if all_authors:
        # Sort contributors by commit count
        author_counts = defaultdict(int)
        for commit in commits:
            author = commit["github_author"]
            if author not in teknium_aliases:
                author_counts[author] += 1
            for coauthor in commit.get("coauthors", []):
                if coauthor not in teknium_aliases:
                    author_counts[coauthor] += 1

        sorted_authors = sorted(author_counts.items(), key=lambda x: -x[1])

        lines.append("## 👥 Contributors")
        lines.append("")
        lines.append("Thank you to everyone who contributed to this release!")
        lines.append("")
        for author, count in sorted_authors:
            commit_word = "commit" if count == 1 else "commits"
            lines.append(f"- {author} ({count} {commit_word})")
        lines.append("")

    # Full changelog link
    if prev_tag:
        lines.append(f"**Full Changelog**: [{prev_tag}...{tag_name}]({repo_url}/compare/{prev_tag}...{tag_name})")
    else:
        lines.append(f"**Full Changelog**: [{tag_name}]({repo_url}/commits/{tag_name})")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Hermes Agent Release Tool")
    parser.add_argument("--bump", choices=["major", "minor", "patch"],
                        help="Which semver component to bump")
    parser.add_argument("--publish", action="store_true",
                        help="Actually create the tag and GitHub release (otherwise dry run)")
    parser.add_argument("--date", type=str,
                        help="Override CalVer date (format: YYYY.M.D)")
    parser.add_argument("--first-release", action="store_true",
                        help="Mark as first release (no previous tag expected)")
    parser.add_argument("--output", type=str,
                        help="Write changelog to file instead of stdout")
    args = parser.parse_args()

    # Determine CalVer date
    if args.date:
        calver_date = args.date
    else:
        now = datetime.now()
        calver_date = f"{now.year}.{now.month}.{now.day}"

    base_tag = f"v{calver_date}"
    tag_name, calver_date = next_available_tag(base_tag)
    if tag_name != base_tag:
        print(f"Note: Tag {base_tag} already exists, using {tag_name}")

    # Determine semver
    current_version = get_current_version()
    if args.bump:
        new_version = bump_version(current_version, args.bump)
    else:
        new_version = current_version

    # Get previous tag
    prev_tag = get_last_tag()
    if not prev_tag and not args.first_release:
        print("No previous tags found. Use --first-release for the initial release.")
        print(f"Would create tag: {tag_name}")
        print(f"Would set version: {new_version}")
        return

    # Get commits
    commits = get_commits(since_tag=prev_tag)
    if not commits:
        print("No new commits since last tag.")
        if not args.first_release:
            return

    print(f"{'='*60}")
    print(f"  Hermes Agent Release Preview")
    print(f"{'='*60}")
    print(f"  CalVer tag:      {tag_name}")
    print(f"  SemVer:          v{current_version} → v{new_version}")
    print(f"  Previous tag:    {prev_tag or '(none — first release)'}")
    print(f"  Commits:         {len(commits)}")
    print(f"  Unique authors:  {len({c['github_author'] for c in commits})}")
    print(f"  Mode:            {'PUBLISH' if args.publish else 'DRY RUN'}")
    print(f"{'='*60}")
    print()

    # Generate changelog
    changelog = generate_changelog(
        commits, tag_name, new_version,
        prev_tag=prev_tag,
        first_release=args.first_release,
    )

    if args.output:
        Path(args.output).write_text(changelog, encoding="utf-8")
        print(f"Changelog written to {args.output}")
    else:
        print(changelog)

    if args.publish:
        print(f"\n{'='*60}")
        print("  Publishing release...")
        print(f"{'='*60}")

        # Update version files
        if args.bump:
            update_version_files(new_version, calver_date)
            print(f"  ✓ Updated version files to v{new_version} ({calver_date})")

            # Commit version bump
            add_files = [str(VERSION_FILE), str(PYPROJECT_FILE)]
            if ACP_REGISTRY_MANIFEST.exists():
                add_files.append(str(ACP_REGISTRY_MANIFEST))
            add_result = git_result("add", *add_files)
            if add_result.returncode != 0:
                print(f"  ✗ Failed to stage version files: {add_result.stderr.strip()}")
                return

            commit_result = git_result(
                "commit", "-m", f"chore: bump version to v{new_version} ({calver_date})"
            )
            if commit_result.returncode != 0:
                print(f"  ✗ Failed to commit version bump: {commit_result.stderr.strip()}")
                return
            print(f"  ✓ Committed version bump")

        # Create annotated tag
        tag_result = git_result(
            "tag", "-a", tag_name, "-m",
            f"Hermes Agent v{new_version} ({calver_date})\n\nWeekly release"
        )
        if tag_result.returncode != 0:
            print(f"  ✗ Failed to create tag {tag_name}: {tag_result.stderr.strip()}")
            return
        print(f"  ✓ Created tag {tag_name}")

        # Push
        push_result = git_result("push", "origin", "HEAD", "--tags")
        if push_result.returncode == 0:
            print(f"  ✓ Pushed to origin")
        else:
            print(f"  ✗ Failed to push to origin: {push_result.stderr.strip()}")
            print("    Continue manually after fixing access:")
            print("    git push origin HEAD --tags")

        # Build semver-named Python artifacts so downstream packagers
        # (e.g. Homebrew) can target them without relying on CalVer tag names.
        artifacts = build_release_artifacts(new_version)
        if artifacts:
            print("  ✓ Built release artifacts:")
            for artifact in artifacts:
                print(f"    - {artifact.relative_to(REPO_ROOT)}")

        # Create GitHub release
        changelog_file = REPO_ROOT / ".release_notes.md"
        changelog_file.write_text(changelog, encoding="utf-8")

        gh_cmd = [
            "gh", "release", "create", tag_name,
            "--title", f"Hermes Agent v{new_version} ({calver_date})",
            "--notes-file", str(changelog_file),
        ]
        gh_cmd.extend(str(path) for path in artifacts)

        gh_bin = shutil.which("gh")
        if gh_bin:
            result = subprocess.run(
                gh_cmd,
                capture_output=True, text=True,
                cwd=str(REPO_ROOT),
            )
        else:
            result = None

        if result and result.returncode == 0:
            changelog_file.unlink(missing_ok=True)
            print(f"  ✓ GitHub release created: {result.stdout.strip()}")
            print(f"\n  🎉 Release v{new_version} ({tag_name}) published!")
        else:
            if result is None:
                print("  ✗ GitHub release skipped: `gh` CLI not found.")
            else:
                print(f"  ✗ GitHub release failed: {result.stderr.strip()}")
            print(f"    Release notes kept at: {changelog_file}")
            print(f"    Tag was created locally. Create the release manually:")
            print(
                f"    gh release create {tag_name} --title 'Hermes Agent v{new_version} ({calver_date})' "
                f"--notes-file .release_notes.md {' '.join(str(path) for path in artifacts)}"
            )
            print(f"\n  ✓ Release artifacts prepared for manual publish: v{new_version} ({tag_name})")
    else:
        print(f"\n{'='*60}")
        print(f"  Dry run complete. To publish, add --publish")
        print(f"  Example: python scripts/release.py --bump minor --publish")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()