package main

import (
	"encoding/json"
	"fmt"
	"io/ioutil"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/keybase/bot-ssh-ca/keybaseca/bot"
	"github.com/keybase/bot-ssh-ca/keybaseca/config"
	klog "github.com/keybase/bot-ssh-ca/keybaseca/log"
	"github.com/keybase/bot-ssh-ca/keybaseca/sshutils"
	"github.com/keybase/bot-ssh-ca/kssh"
	"github.com/keybase/bot-ssh-ca/shared"

	"github.com/urfave/cli"
)

func main() {
	app := cli.NewApp()
	app.Name = "keybaseca"
	app.Usage = "An SSH CA built on top of Keybase"
	app.Version = "0.0.1"
	app.Flags = []cli.Flag{
		cli.StringFlag{
			Name:  "config, c",
			Value: config.DefaultConfigLocation,
			Usage: "Load configuration from `FILE`",
		},
		cli.BoolFlag{
			Name:   "wipe-all-configs",
			Hidden: true,
			Usage:  "Used in the integration tests to clean all client configs from KBFS",
		},
		cli.BoolFlag{
			Name:   "wipe-logs",
			Hidden: true,
			Usage:  "Used in the integration tests to delete all CA logs",
		},
	}
	app.Commands = []cli.Command{
		{
			Name:  "backup",
			Usage: "Print the current CA private key to stdout for backup purposes",
			Action: func(c *cli.Context) error {
				fmt.Println("Are you sure you want to export the CA private key? If this key is compromised, an " +
					"attacker could access every server that you have configured with this bot. Type \"yes\" to confirm.")
				var response string
				_, err := fmt.Scanln(&response)
				if err != nil {
					return err
				}
				if response != "yes" {
					return fmt.Errorf("Did not get confirmation of key export, aborting...")
				}

				conf, err := loadServerConfig(c.GlobalString("config"))
				if err != nil {
					return err
				}
				bytes, err := ioutil.ReadFile(conf.GetCAKeyLocation())
				if err != nil {
					return fmt.Errorf("Failed to load the CA key from %s: %v", conf.GetCAKeyLocation(), err)
				}
				klog.Log(conf, "Exported CA key to stdout")
				fmt.Println("\nKeep this key somewhere very safe. We recommend keeping a physical copy of it in a secure place.")
				fmt.Println("")
				fmt.Println(string(bytes))
				return nil
			},
		},
		{
			Name:  "generate",
			Usage: "Generate a new CA key",
			Flags: []cli.Flag{
				cli.BoolFlag{
					Name: "overwrite-existing-key",
				},
			},
			Action: func(c *cli.Context) error {
				conf, err := loadServerConfigAndWriteClientConfig(c.GlobalString("config"))
				if err != nil {
					return err
				}
				captureControlCToDeleteClientConfig(conf)
				defer deleteClientConfig(conf)
				err = sshutils.Generate(conf, c.Bool("overwrite-existing-key") || os.Getenv("FORCE_WRITE") == "true", true)
				if err != nil {
					return fmt.Errorf("Failed to generate a new key: %v", err)
				}
				return nil
			},
		},
		{
			Name:  "service",
			Usage: "Start the CA service in the foreground",
			Action: func(c *cli.Context) error {
				conf, err := loadServerConfigAndWriteClientConfig(c.GlobalString("config"))
				if err != nil {
					return err
				}
				captureControlCToDeleteClientConfig(conf)
				defer deleteClientConfig(conf)
				err = bot.StartBot(conf)
				if err != nil {
					return fmt.Errorf("CA chatbot crashed: %v", err)
				}
				return nil
			},
			Flags: []cli.Flag{},
		},
	}
	app.Action = func(c *cli.Context) error {
		if c.Bool("wipe-all-configs") {
			teams, err := shared.KBFSList("/keybase/team/")
			if err != nil {
				return err
			}

			semaphore := make(chan interface{}, len(teams))
			for _, team := range teams {
				go func(team string) {
					filename := fmt.Sprintf("/keybase/team/%s/%s", team, shared.ConfigFilename)
					exists, _ := shared.KBFSFileExists(filename)
					if exists {
						err = shared.KBFSDelete(filename)
						if err != nil {
							fmt.Printf("%v\n", err)
						}
					}
					semaphore <- 0
				}(team)
			}
			for i := 0; i < len(teams); i++ {
				<-semaphore
			}
		}
		if c.Bool("wipe-logs") {
			conf, err := loadServerConfig(c.String("config"))
			if err != nil {
				return err
			}
			logLocation := conf.GetLogLocation()
			if strings.HasPrefix(logLocation, "/keybase/") {
				err = shared.KBFSDelete(logLocation)
				if err != nil {
					return fmt.Errorf("Failed to delete log file at %s: %v", logLocation, err)
				}
			} else {
				err = os.Remove(logLocation)
				if err != nil {
					return fmt.Errorf("Failed to delete log file at %s: %v", logLocation, err)
				}
			}
			fmt.Println("Wiped existing log file at " + logLocation)
		}
		return nil
	}
	err := app.Run(os.Args)
	if err != nil {
		log.Fatal(err)
	}
}

// Write a kssh config file to /keybase/team/teamname.ssh/kssh-client.config. kssh will automatically pick up
// and use this config
func writeClientConfig(conf config.Config) error {
	username, err := bot.GetUsername(conf)
	if err != nil {
		return err
	}

	for _, team := range conf.GetTeams() {
		filename := filepath.Join("/keybase/team/", team, shared.ConfigFilename)

		content, err := json.Marshal(kssh.ConfigFile{TeamName: team, BotName: username, ChannelName: conf.GetChannelName()})
		if err != nil {
			return err
		}

		err = shared.KBFSWrite(filename, string(content), false)
		if err != nil {
			return err
		}
	}

	return nil
}

// Delete the client config file. Run when the CA bot is terminating so that KBFS does not contain any stale
// client config files
func deleteClientConfig(conf config.Config) error {
	for _, team := range conf.GetTeams() {
		filename := filepath.Join("/keybase/team/", team, shared.ConfigFilename)
		err := shared.KBFSDelete(filename)
		if err != nil {
			return err
		}
	}
	return nil
}

func captureControlCToDeleteClientConfig(conf config.Config) {
	signalChan := make(chan os.Signal)
	signal.Notify(signalChan, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-signalChan
		fmt.Println("losing CA bot...")
		err := deleteClientConfig(conf)
		if err != nil {
			fmt.Printf("Failed to delete client config: %v", err)
			os.Exit(1)
		}
		os.Exit(0)
	}()
}

func loadServerConfig(configFilename string) (config.Config, error) {
	if _, err := os.Stat(configFilename); os.IsNotExist(err) {
		return nil, fmt.Errorf("Config file at %s does not exist", configFilename)
	}
	conf, err := config.LoadConfig(configFilename)
	if err != nil {
		return nil, fmt.Errorf("Failed to parse config file: %v", err)
	}
	return conf, nil
}

func loadServerConfigAndWriteClientConfig(configFilename string) (config.Config, error) {
	conf, err := loadServerConfig(configFilename)
	if err != nil {
		return nil, err
	}
	err = writeClientConfig(conf)
	if err != nil {
		return nil, fmt.Errorf("Failed to write the client config: %v", err)
	}
	return conf, nil
}
