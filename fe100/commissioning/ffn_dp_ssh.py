import shlex,subprocess,sys
args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts','-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2',shlex.join(sys.argv[1:])]
raise SystemExit(subprocess.call(args))
