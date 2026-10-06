from lifecycle_service.core import WorkOrder, summarize


if __name__ == "__main__":
    record = WorkOrder.create("demo", "v1", "draft", "operator", {"场景": "登记服务工单并生成工单摘要"})
    print(summarize(record))

