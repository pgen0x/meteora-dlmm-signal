// Command meteora-dlmm-signal is a standalone daemon that continuously watches
// the Meteora pool-discovery API, screens pools with the same gates the DLMM
// pipeline skill uses, and forwards each newly-qualifying pool to a Hermes agent
// webhook. The agent then reviews the signal and decides whether to open a
// concentrated-liquidity position.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/meteora-dlmm-signal/internal/config"
	"github.com/meteora-dlmm-signal/internal/scanner"
)

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	cfg := config.Load()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
		<-ch
		log.Println("shutdown signal received")
		cancel()
	}()

	scanner.New(cfg).Run(ctx)
}
