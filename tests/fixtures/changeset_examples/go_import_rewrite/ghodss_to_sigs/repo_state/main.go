package main

import (
	"fmt"

	"github.com/ghodss/yaml"
)

func main() {
	data, err := yaml.Marshal(map[string]string{"hello": "world"})
	if err != nil {
		panic(err)
	}
	fmt.Println(string(data))
}
