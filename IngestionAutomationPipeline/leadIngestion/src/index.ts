import { createApp } from "./app";
import { env } from "./config/env";
import { logger } from "./utils/logger";
import { loadSalesTeam } from "./services/salesTeam.service";
import "./db/database"; // initializes the sqlite connection + runs schema migration on import

function bootstrap() {
  // Fail fast if the roster file is missing/invalid, rather than on the first request.
  loadSalesTeam();

  const app = createApp();
  app.listen(env.port, () => {
    logger.info(`lead-ingest-service listening on port ${env.port}`);
  });
}

bootstrap();
