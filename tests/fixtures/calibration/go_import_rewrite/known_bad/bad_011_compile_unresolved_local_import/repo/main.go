package main

import (
	"fmt"

	// Intentionally wrong intra-module import path — this subpackage
	// does not exist in this module. Mirrors a real post-rewrite bug
	// where an automated import rewriter produced a path that points
	// nowhere.
	"example.com/calib/bad011/missingsub"
)

func main() {
	fmt.Println(missingsub.Hello)
}
