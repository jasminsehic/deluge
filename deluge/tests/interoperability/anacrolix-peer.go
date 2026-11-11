// Independent Pion/WebTorrent peer used to validate Deluge/libtorrent interoperability.
// Built inside the pinned jasminsehic/torrent checkout, not inside Deluge.
package main

import (
    "context"
    "flag"
    "fmt"
    "log"
    "os"
    "path/filepath"
    "time"

    "github.com/anacrolix/torrent"
    "github.com/anacrolix/torrent/metainfo"
)

func run(mode, torrentPath, dataDir string, size int64) error {
    if err := os.MkdirAll(dataDir, 0755); err != nil {
        return err
    }
    meta, err := metainfo.LoadFromFile(torrentPath)
    if err != nil {
        return fmt.Errorf("load metainfo: %w", err)
    }
    cfg := torrent.NewDefaultClientConfig()
    cfg.DataDir = dataDir
    cfg.Seed = true
    cfg.DisableTCP = true
    cfg.DisableUTP = true
    cfg.NoDHT = true
    cfg.DisablePEX = true
    cfg.DisableWebseeds = true
    cfg.NoDefaultPortForwarding = true
    cfg.DisableIPv6 = true
    cfg.DisableWebtorrent = false
    cfg.ListenPort = 0
    cfg.LocalServiceDiscovery = nil
    cfg.ICEServers = nil
    cfg.ICEServerList = nil

    client, err := torrent.NewClient(cfg)
    if err != nil {
        return fmt.Errorf("new client: %w", err)
    }
    defer client.Close()
    t, err := client.AddTorrent(meta)
    if err != nil {
        return fmt.Errorf("add torrent: %w", err)
    }
    defer t.Drop()

    if mode == "seed" {
        ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
        defer cancel()
        if err := t.VerifyDataContext(ctx); err != nil {
            return fmt.Errorf("verify seed data: %w", err)
        }
        if t.BytesCompleted() != size {
            return fmt.Errorf("seed data incomplete: %d / %d bytes (%s)",
                t.BytesCompleted(), size, filepath.Join(dataDir, "interop.bin"))
        }
    } else if mode == "download" {
        t.DownloadAll()
    } else {
        return fmt.Errorf("unsupported mode: %s", mode)
    }
    fmt.Printf("READY %s\n", mode)

    deadline := time.NewTimer(180*time.Second)
    ticker := time.NewTicker(500*time.Millisecond)
    defer deadline.Stop()
    defer ticker.Stop()

    for {
        select {
        case <-deadline.C:
            return fmt.Errorf("timeout: %s completed %d / %d bytes",
                mode, t.BytesCompleted(), size)
        case <-ticker.C:
            if mode == "download" && t.BytesCompleted() >= size {
                fmt.Printf("COMPLETE %d\n", t.BytesCompleted())
                return nil
            }
        }
    }
}

func main() {
    mode := flag.String("mode", "", "seed or download")
    torrentPath := flag.String("torrent", "", "path to torrent metainfo")
    dataDir := flag.String("data", "", "directory for torrent payload")
    size := flag.Int64("size", 0, "expected torrent payload bytes")
    flag.Parse()
    if err := run(*mode, *torrentPath, *dataDir, *size); err != nil {
        log.Fatal(err)
    }
}
