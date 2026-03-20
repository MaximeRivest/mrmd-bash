package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/MaximeRivest/mrmd-bash/internal/mrpbash"
)

func main() {
	var host string
	var port int
	var cwd string
	var reload bool
	var logLevel string

	flag.StringVar(&host, "host", "127.0.0.1", "Host to bind to")
	flag.IntVar(&port, "port", 8001, "Port to bind to")
	flag.StringVar(&cwd, "cwd", "", "Working directory for the bash runtime")
	flag.BoolVar(&reload, "reload", false, "Accepted for compatibility; no-op in Go implementation")
	flag.StringVar(&logLevel, "log-level", "info", "Log level: debug, info, warning, error")
	flag.Parse()

	resolvedCWD := cwd
	if resolvedCWD == "" {
		wd, err := os.Getwd()
		if err != nil {
			log.Fatalf("getwd: %v", err)
		}
		resolvedCWD = wd
	}

	resolvedCWD, err := filepath.Abs(resolvedCWD)
	if err != nil {
		log.Fatalf("resolve cwd: %v", err)
	}

	if reload {
		log.Printf("warning: --reload is a no-op in the Go implementation")
	}

	logger := log.New(os.Stdout, "", log.LstdFlags)
	server := mrpbash.NewServer(resolvedCWD, logLevel, logger)
	addr := fmt.Sprintf("%s:%d", host, port)

	fmt.Println("Starting mrmd-bash server...")
	fmt.Printf("  Host: %s\n", host)
	fmt.Printf("  Port: %d\n", port)
	fmt.Printf("  Working directory: %s\n", resolvedCWD)
	fmt.Printf("  URL: http://%s/mrp/v1/capabilities\n\n", addr)

	httpServer := &http.Server{
		Addr:    addr,
		Handler: server.Handler(),
	}

	shutdownCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		<-shutdownCtx.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
		_ = httpServer.Shutdown(ctx)
	}()

	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("listen: %v", err)
	}
}
