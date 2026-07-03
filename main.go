// Command meteora-dlmm-trading-bot is a standalone daemon that continuously watches
// the Meteora pool-discovery API, screens pools with the same gates the DLMM
// pipeline skill uses, and forwards each newly-qualifying pool to a Hermes agent
// webhook. The agent then reviews the signal and decides whether to open a
// concentrated-liquidity position.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/meteora-dlmm-trading-bot/internal/config"
	"github.com/meteora-dlmm-trading-bot/internal/scanner"
)

// Version follows Semantic Versioning (semver.org). Bump it alongside a
// CHANGELOG.md entry and a matching `vX.Y.Z` git tag — see CONTRIBUTING.md.
const Version = "0.1.0"

func main() {
	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println("mds " + Version)
		return
	}

	log.SetFlags(log.LstdFlags | log.LUTC)
	log.Printf("meteora-dlmm-trading-bot %s starting", Version)
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
