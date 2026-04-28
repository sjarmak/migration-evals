package main

import (
	"fmt"
	"slices"
)

func main() {
	xs := []int{3, 1, 2}
	slices.Sort(xs)
	fmt.Println(xs)
}
