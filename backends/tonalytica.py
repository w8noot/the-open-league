from typing import Dict

from models.backend import CalculationBackend
from models.backends import BACKEND_TONALYTICA
from models.metric import MetricImpl, CalculationContext
from models.results import ProjectStat, CalculationResults
from models.season_config import SeasonConfig
import psycopg2
import psycopg2.extras
from loguru import logger

class TonalyticaAppBackend(CalculationBackend):
    def __init__(self):
        CalculationBackend.__init__(self, "Tonalytica App leaderboard",
                                    leaderboards=[SeasonConfig.APPS])

    def _do_calculate(self, config: SeasonConfig, dry_run: bool = False):
        logger.info("Running tonalytica App leaderboard SQL generation")
        PROJECTS = []
        PROJECTS_ALIASES = []
        context = CalculationContext(season=config, impl=BACKEND_TONALYTICA)
        
        for project in config.projects:
            context.project = project
            metrics = []
            for metric in project.metrics:
                metrics.append(metric.calculate(context))
            metrics = "\nUNION ALL\n".join(metrics)
            PROJECTS.append(f"""
            project_{project.name} as (
            {metrics}
            )
            """)
            PROJECTS_ALIASES.append(f"""
            select * from project_{project.name}
            """)
        PROJECTS = ",\n".join(PROJECTS)
        PROJECTS_ALIASES = "\nUNION ALL\n".join(PROJECTS_ALIASES)
        SQL = f"""
        with messages_local as (
            -- we will use subset of messages table for better performance
            -- also this table contains only messages with successful destination tx
            select * from tol.messages_{config.safe_season_name()}
        ),
        {PROJECTS},
        all_projects_raw as (
        {PROJECTS_ALIASES}        
        ),
        all_projects as (
          -- exclude banned users
         select f.* from all_projects_raw f
         left join tol.banned_users b on b.address = f.user_address -- exclude banned users
         where b.address is null
        )
        , users_stats_raw as (
          select project, user_address, min(weight) as weight, count(distinct id) as tx_count from all_projects
          group by 1, 2
        ), users as (
         select distinct user_address from users_stats_raw
        ),
        states as (
          -- get code hash 
         select distinct on (as2.address)  usr.user_address, code_hash from account_state as2
         join users usr on usr.user_address = as2.address
         order by address, last_tx_lt desc
        ), wallets as (
         select user_address from states where
         code_hash is null or
           code_hash = '/rX/aCDi/w2Ug+fg1iyBfYRniftK5YDIeIZtlZ2r1cA='   or   -- wallet v4 r2
           code_hash = 'hNr6RJ+Ypph3ibojI1gHK8D3bcRSQAKl0JGLmnXS1Zk='   or   -- wallet v3 r2
           code_hash = 'thBBpYp5gLlG6PueGY48kE0keZ/6NldOpCUcQaVm9YE='   or   -- wallet v3 r1
           code_hash = 'ZN1UgFUixb6KnbWc6gEFzPDQh4bKeb64y3nogKjXMi0='   or   -- wallet v4 r1
           code_hash = 'MZrVLsmoWWIPil2Ww2CJ5nw29OOTAdBQ224VCXAZzpE='   or   -- wallet_v5_beta
           code_hash = 'WHzHie/xyE9G7DeX5F/ICaFP9a4k8eDHpqmcydyQYf8='   or   -- wallet v1 r3
           code_hash = 'XJpeaMEI4YchoHxC+ZVr+zmtd+xtYktgxXbsiO7mUyk='   or   -- wallet v2 r1
           code_hash = '/pUw0yQ4Uwg+8u8LTCkIwKv2+hwx6iQ6rKpb+MfXU/E='   or   -- wallet v2 r2
           code_hash = 'oM/CxIruFqJx8s/AtzgtgXVs7LEBfQd/qqs7tgL2how='   or   -- wallet v1 r1
           code_hash = '1JAvzJ+tdGmPqONTIgpo2g3PcuMryy657gQhfBfTBiw='        -- wallet v1 r2
        ),
        users_stats as (
          select * from users_stats_raw
          join wallets using(user_address)
        )
        , good_users as (
        select project, sum(weight) as total_users from users_stats
        where tx_count > 1
        group by 1
        ), tx_stat as (
        select project, sum(weight * tx_count) as tx_count from users_stats
        group by 1
        )
        select project, tx_count,  coalesce(total_users,0 )as total_users
        from tx_stat
                 left join good_users using(project)
        """
        logger.info(f"Generated SQL: {SQL}")

        results: Dict[str, ProjectStat] = {}
        with psycopg2.connect() as pg:
            if dry_run:
                logger.info("Running SQL query in dry_run mode")
                with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute(f"explain {SQL}")
            else:
                logger.info("Running SQL query in production mode")
                with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute(SQL)
                    for row in cursor.fetchall():
                        logger.info(row)
                        assert row['project'] not in results
                        results[row['project']] = ProjectStat(
                            name=row['project'],
                            metrics={}
                        )
                        results[row['project']].metrics['tx_count'] = int(row['tx_count'])
                        results[row['project']].metrics['total_users'] = int(row['total_users'])
                logger.info("Main query finished")
            if not dry_run:
                logger.info("Requesting off-chain tganalytics.xyz metrics")
                with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    for project in config.projects:
                        logger.info(f"Requesting data for {project.name} ({project.analytics_key}) ({config.name})")
                        cursor.execute("""
                        select * from tol.tganalytics_latest where app_name = %s and season = %s
                        """, (project.analytics_key, config.name))
                        res = cursor.fetchone()
                        if not res:
                            logger.error(f"No off-chain data for {project.name}")
                        else:
                            if project.name not in results:
                                logger.error(f"Project {project.name} has no on-chain data, ignoring")
                            else:
                                results[project.name].metrics['non_premium_users'] = int(res['non_premium_users'])
                                results[project.name].metrics['premium_users'] = int(res['premium_users'])

                logger.info("Off-chain processing is finished")

        return CalculationResults(ranking=results.values(), build_time=1)  # TODO build time


    def _generate_project_block(self, config: SeasonConfig, metric: MetricImpl):
        return metric.calculate(config)