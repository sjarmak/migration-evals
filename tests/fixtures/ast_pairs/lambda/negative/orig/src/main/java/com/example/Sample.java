package com.example;

public class Sample {
    public Runnable factory() {
        return new Runnable() {
            @Override
            public void run() {
                System.out.println("hi");
            }
        };
    }
}
