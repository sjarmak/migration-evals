package main

import "fmt"

func main() {
	// fmt has no NotARealFunction — go build will fail with
	// "undefined: fmt.NotARealFunction". A realistic shape for
	// import-rewrite breakage where the new package's API differs
	// from the old one's.
	fmt.NotARealFunction("oops")
}
