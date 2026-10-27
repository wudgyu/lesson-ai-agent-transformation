package com.example.order.config;

import com.zaxxer.hikari.HikariDataSource;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.DataSource;

/**
 * HikariCP 连接池配置。
 * order-service 的连接池名为 HikariPool-1，最大连接数 50。
 */
@Configuration
public class DataSourceConfig {

    @Bean
    public DataSource dataSource() {
        HikariDataSource ds = new HikariDataSource();
        ds.setMaximumPoolSize(50);
        ds.setMinimumIdle(10);
        ds.setConnectionTimeout(3000);
        ds.setIdleTimeout(600000);
        ds.setLeakDetectionThreshold(20000);
        ds.setPoolName("HikariPool-1");
        return ds;
    }
}
