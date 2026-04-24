package com.example;

public class Sample {
    public String render() {
        String sql = "SELECT *\n" +
            "FROM users\n" +
            "WHERE id = ?\n";
        return sql;
    }
}
