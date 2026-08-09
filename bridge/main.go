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
	"bufio"
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
var Version = "0.6.2"

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
	IsAdmin  bool   `json:"is_admin"` // 当前是否以管理员权限运行
}

func main() {
	var serverURL, roomCode string
	var elevate, elevated bool
	flag.StringVar(&serverURL, "server", "ws://localhost:8000", "服务器地址 (ws:// 或 wss://)")
	flag.StringVar(&roomCode, "room", "", "房间码（必填）")
	flag.BoolVar(&elevate, "elevate", false, "自动请求管理员权限（Windows UAC 提权）")
	flag.BoolVar(&elevated, "elevated", false, "内部标志：已处于提权后的进程")
	flag.Parse()

	// 交互模式：双击运行 / 未提供房间码时，引导用户输入
	// 而不是直接报错退出（避免"闪退"）
	if roomCode == "" {
		reader := bufio.NewReader(os.Stdin)
		fmt.Println("==============================================")
		fmt.Println(" 云端AI远程运维助手 · 桥接器 v" + Version)
		fmt.Println("----------------------------------------------")
		fmt.Println(" 未检测到房间码，请输入连接信息：")
		fmt.Println("")

		if serverURL == "ws://localhost:8000" || flag.NFlag() == 0 {
			fmt.Print(" 服务器地址 [回车默认 ws://106.54.193.9:8000]: ")
			input, _ := reader.ReadString('\n')
			input = strings.TrimSpace(input)
			if input != "" {
				serverURL = input
			} else {
				serverURL = "ws://106.54.193.9:8000"
			}
		}

		fmt.Print(" 房间码 (6位, 例如 MUJRWQ): ")
		room, _ := reader.ReadString('\n')
		room = strings.TrimSpace(strings.ToUpper(room))
		if room == "" {
			fmt.Fprintln(os.Stderr, "错误: 房间码不能为空")
			fmt.Println("按回车键退出...")
			_, _ = reader.ReadString('\n')
			os.Exit(2)
		}
		roomCode = room
		fmt.Println("----------------------------------------------")
		fmt.Printf(" 即将连接服务器 %s · 房间 %s\n", serverURL, roomCode)
		fmt.Println("==============================================")
		fmt.Println("")
	}

	// 管理员提权（仅 Windows 有意义）：
	//   条件：非管理员 + 尚未提权 + (命令行 --elevate 或 交互模式询问确认)
	//   提权后新进程带 --elevated 标志自动跳过，防递归
	if !isAdmin() && !elevated {
		shouldElevate := elevate
		if !shouldElevate && flag.NFlag() == 0 {
			// 交互模式（无任何参数）：询问用户是否提权
			reader := bufio.NewReader(os.Stdin)
			fmt.Print(" 当前不是管理员权限，部分功能受限（如读取完整 BIOS 设置）。\n 是否以管理员身份重新启动？[Y/n]: ")
			answer, _ := reader.ReadString('\n')
			answer = strings.TrimSpace(strings.ToLower(answer))
			shouldElevate = answer != "n" && answer != "no" // 回车/其他 = 默认提权
		}
		if shouldElevate {
			fmt.Println(" 正在请求管理员权限（UAC 弹窗确认后自动继续）…")
			if err := elevateSelf(serverURL, roomCode); err != nil {
				fmt.Fprintf(os.Stderr, " 提权失败: %v\n", err)
				fmt.Println(" 继续以普通权限运行（部分功能受限）…")
			} else {
				// 提权成功：新进程已启动，本进程退出
				fmt.Println(" 已触发提权，请在新的管理员窗口中查看连接状态。")
				os.Exit(0)
			}
		} else {
			fmt.Println(" 已选择普通权限运行（部分功能受限，如 BIOS 全量读取）。")
		}
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
		IsAdmin:  isAdmin(),
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
