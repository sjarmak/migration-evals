package com.example;

import java.util.Optional;

public class Sample {
    public static class Holder {
        public String name;
        public Holder(String n) { this.name = n; }
    }

    public void handle(Holder user1) {
        Optional.ofNullable(user1)
            .map(h -> h.name)
            .ifPresent(System.out::println);
    }
}
