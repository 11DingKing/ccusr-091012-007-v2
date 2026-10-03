"""批量生成处置计划的管理命令：python manage.py generate_disposal_plans [--as-of YYYY-MM-DD]"""
from datetime import datetime

from django.core.management.base import BaseCommand

from apps.warehouse.retention_cron import generate_disposal_plans


class Command(BaseCommand):
    help = '按入库事实为在管物资（重新）计算处置资格并生成处置计划'

    def add_arguments(self, parser):
        parser.add_argument('--as-of', dest='as_of', default=None,
                            help='计算基准日 YYYY-MM-DD，默认今天')
        parser.add_argument('--case-type', dest='case_type', default=None)
        parser.add_argument('--category', dest='category', type=int, default=None)

    def handle(self, *args, **options):
        as_of = (
            datetime.strptime(options['as_of'], '%Y-%m-%d').date()
            if options['as_of'] else None
        )
        stats = generate_disposal_plans(
            as_of=as_of,
            case_type=options['case_type'],
            category_id=options['category'],
        )
        self.stdout.write(self.style.SUCCESS(
            f"已生成 {stats['total']} 份处置计划（基准日 {stats['as_of']}）："
            f"可销毁 {stats['eligible']}，暂停中 {stats['suspended']}，"
            f"延期中 {stats['extended']}，顺延期 {stats['tolled']}，"
            f"正常 {stats['normal']}，永久 {stats['permanent']}"
        ))
