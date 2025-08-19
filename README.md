# RedisMap 🛡️🔥

RedisMap is an **advanced Redis enumeration & exploitation tool** for penetration testers and security researchers. It allows you to enumerate databases, dump keys, retrieve values, and interact with Redis servers using multi-threading and delays. Perfect for labs, learning, and ethical hacking exercises.

---

## ⚡ Features

- Detect number of Redis databases  
- Dump keys with `--keys`  
- Dump key values with `--values` (auto-detects type: string, hash, etc.)  
- Dump all databases with `--dump-all`  
- Interactive Redis shell: `--shell`  
- Multi-threaded scanning with `--threads`  
- Request timing delays with `-T1`, `-T2`, … (1 sec, 2 sec per request)  
- Reports saved in `reports/` as `{target}_report.txt`  
- Random bold banner with colors at startup  

---

## 🛠️ Installation

```bash
git clone https://github.com/giriaryan694-a11y/redismap.git
cd RedisMap
pip install -r requirements.txt

# RedisMap - Usage examples (copy/paste in your terminal)

# Basic help
python3 redismap.py -h

# Advanced help (examples)
python3 redismap.py --help

# Ping / simple connection check (no flags, just validate connectivity)
python3 redismap.py -t 192.168.1.100

# List keys in DB0
python3 redismap.py -t 192.168.1.100 --keys

# Dump keys with values in DB0
python3 redismap.py -t 192.168.1.100 --values

# Dump all DBs (DB0..DB15) without values
python3 redismap.py -t 192.168.1.100 --dump-all

# Dump all DBs with values (use --values together with --dump-all)
python3 redismap.py -t 192.168.1.100 --dump-all --values

# Use interactive Redis shell (type 'exit' to quit)
python3 redismap.py -t 192.168.1.100 --shell

# Use a custom Redis port (e.g. 6380)
python3 redismap.py -t 192.168.1.100 -p 6380 --dump-all --values

# Multi-threaded dump-all (example: 4 threads)
python3 redismap.py -t 192.168.1.100 --dump-all --values --threads 4

# Add delay between requests (1 second)
python3 redismap.py -t 192.168.1.100 --dump-all --values -T 1

# Combine options: multi-threaded, with delay, custom port
python3 redismap.py -t 192.168.1.100 -p 6380 --dump-all --values --threads 4 -T 2

# Output: reports are saved automatically to reports/<target>_report.txt
# e.g. after a run:
#    reports/192.168.1.100_report.txt
