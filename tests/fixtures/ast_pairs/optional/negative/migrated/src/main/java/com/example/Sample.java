package com.example;

public class Sample {
    public static class Holder {
        public String name;
        public Holder(String n) { this.name = n; }
    }

    public void handle(Holder user1) {
        if (user1 != null) {
            String n = user1.name;
            if (n != null) {
                System.out.println(n);
            }
        }
    }
}
