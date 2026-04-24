package com.example;

public class Sample {
    public Runnable factory() {
        return () -> {
            System.out.println("hi");
        };
    }
}
