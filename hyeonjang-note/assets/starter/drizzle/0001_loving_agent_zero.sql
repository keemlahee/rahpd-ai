CREATE TABLE `billing_orders` (
	`order_id` text PRIMARY KEY NOT NULL,
	`user_id` text NOT NULL,
	`amount` integer NOT NULL,
	`status` text NOT NULL,
	`payment_key` text,
	`period_start` text,
	`period_end` text,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_billing_orders_user_created` ON `billing_orders` (`user_id`,`created_at`);--> statement-breakpoint
CREATE TABLE `subscriptions` (
	`user_id` text PRIMARY KEY NOT NULL,
	`customer_key` text NOT NULL,
	`billing_key` text,
	`paid_until` text,
	`status` text DEFAULT 'PENDING' NOT NULL,
	`cancel_at_period_end` integer DEFAULT 0 NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `subscriptions_customer_key_unique` ON `subscriptions` (`customer_key`);