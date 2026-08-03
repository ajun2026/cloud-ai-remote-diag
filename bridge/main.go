// clouddiag-bridge — 云端AI远程运维助手 · 透明管道化桥接器
//
// 设计原则（v2 管道化）：
//   1. 单一职责：只做「命令管道 + 文件通道」，把服务器的命令送进本地 shell，
//      把结果送回来。不内置任何业务工具。
//   2. 平台无关：同一份代码交叉编译出 Windows / Linux / macOS 版本。
//   3. 透明可审计：本地日志记录每一条执行过的命令；控制台显示连接状态；
//      不静默后台、不请求管理员权限、不自启动。
//   4. 行为面最小：能靠执行命令实现的能力，一律不内置。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
)

// Version 桥接器版本号（构建时可用 -ldflags 覆盖）
var Version = "0.5.0"

// ClientInfo 上报给服务器的本机身份信息
type ClientInfo struct {
	Hostname string `json:"hostname"`
	OS       string `json:"os"`
	Platform string `json:"platform"` // windows | linux | darwin
	Arch     string `json:"arch"`
	LocalIP  string `json:"local_ip"`
	Username string `json:"username"`
	Version  string `json:"version"`
	Bridge   string `json:"bridge"` // "go-pipe"
}

func main() {
	var serverURL, roomCode string
	flag.StringVar(&serverURL, "server", "ws://localhost:8000", "服务器地址 (ws:// 或 wss://)")
	flag.StringVar(&roomCode, "room", "", "房间码（必填）")
	flag.Parse()

	if roomCode == "" {
		fmt.Fprintln(os.Stderr, "错误: 缺少房间码，用法: bridge -server ws://host:8000 -room 房间码")
		os.Exit(2)
	}
	serverURL = strings.TrimRight(serverURL, "/")

	logger := NewAuditLogger()
	info := collectClientInfo()
	logger.Info("bridge %s 启动 (pid=%d, os=%s/%s, host=%s)",
		Version, os.Getpid(), info.Platform, info.Arch, info.Hostname)

	// 注册退出信号，优雅断开
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)

	cfg := &Config{
		ServerURL: serverURL,
		RoomCode:  roomCode,
		Info:      info,
		Logger:    logger,
	}

	c := NewClient(cfg)

	go func() {
		<-sig
		logger.Info("收到退出信号，正在断开…")
		c.Shutdown()
		os.Exit(0)
	}()

	// 阻塞运行（内部自动重连）
	c.Run()
}

// collectClientInfo 采集本机身份信息（纯只读，无副作用）
func collectClientInfo() ClientInfo {
	hostname, _ := os.Hostname()
	username := os.Getenv("USER")
	if username == "" {
		username = os.Getenv("USERNAME")
	}
	if username == "" {
		username = "unknown"
	}
	return ClientInfo{
		Hostname: hostname,
		OS:       runtime.GOOS,
		Platform: runtime.GOOS,
		Arch:     runtime.GOARCH,
		LocalIP:  localIP(),
		Username: username,
		Version:  Version,
		Bridge:   "go-pipe",
	}
}

// localIP 取本机第一块非回环 IPv4 地址（尽力而为）
func localIP() string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 || iface.Flags&net.FlagUp == 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ip := strings.Split(a.String(), "/")[0]
			if strings.Contains(ip, ":") {
				continue
			}
			return ip
		}
	}
	return ""
}

// MarshalJSON 调试用：打印消息时不输出敏感内容
func debugJSON(v interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprintf("<json err: %v>", err)
	}
	if len(b) > 400 {
		return string(b[:400]) + "…(truncated)"
	}
	return string(b)
}
