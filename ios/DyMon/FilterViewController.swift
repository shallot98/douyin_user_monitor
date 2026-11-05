import UIKit

/// 筛选：排序 / 擦边分下限 / 关键词（对应作品库接口的同名参数）
final class FilterViewController: UIViewController {

    var onApply: ((FeedConfig) -> Void)?

    private var config: FeedConfig
    private let sortButton = UIButton(type: .system)
    private let scoreControl = UISegmentedControl(items: ["不限", "1", "2", "3", "4", "5"])
    private let keywordField = UITextField()

    init(config: FeedConfig) {
        self.config = config
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) 未实现") }

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "筛选条件"
        overrideUserInterfaceStyle = .dark
        view.backgroundColor = .systemBackground
        navigationItem.leftBarButtonItem = UIBarButtonItem(title: "取消", style: .plain, target: self, action: #selector(cancel))
        navigationItem.rightBarButtonItem = UIBarButtonItem(title: "应用", style: .done, target: self, action: #selector(apply))
        buildUI()
    }

    private func buildUI() {
        var menuItems: [UIAction] = []
        for sort in LibraryQuery.sorts {
            menuItems.append(UIAction(title: sort.title, state: sort.value == config.sort ? .on : .off) { [weak self] _ in
                self?.config.sort = sort.value
                self?.refreshSortTitle()
            })
        }
        var sortConfig = UIButton.Configuration.gray()
        sortConfig.title = config.sortTitle
        sortConfig.baseForegroundColor = .label
        sortConfig.cornerStyle = .medium
        sortButton.configuration = sortConfig
        sortButton.menu = UIMenu(title: "排序", children: menuItems)
        sortButton.showsMenuAsPrimaryAction = true

        scoreControl.selectedSegmentIndex = config.scoreMin.map { min(max($0, 1), 5) } ?? 0

        keywordField.placeholder = "关键词（描述里包含）"
        keywordField.text = config.keyword
        keywordField.borderStyle = .roundedRect
        keywordField.autocorrectionType = .no
        keywordField.autocapitalizationType = .none

        let stack = UIStackView(arrangedSubviews: [
            row(title: "排序", control: sortButton),
            row(title: "擦边分下限", control: scoreControl),
            row(title: "关键词", control: keywordField),
        ])
        stack.axis = .vertical
        stack.spacing = 22
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 24),
        ])
    }

    private func row(title: String, control: UIView) -> UIView {
        let label = UILabel()
        label.text = title
        label.font = .systemFont(ofSize: 14, weight: .medium)
        label.textColor = .secondaryLabel
        let stack = UIStackView(arrangedSubviews: [label, control])
        stack.axis = .vertical
        stack.spacing = 8
        return stack
    }

    private func refreshSortTitle() {
        sortButton.configuration?.title = config.sortTitle
        sortButton.menu?.children.forEach { item in
            guard let action = item as? UIAction else { return }
            action.state = action.title == config.sortTitle ? .on : .off
        }
    }

    @objc private func cancel() {
        dismiss(animated: true)
    }

    @objc private func apply() {
        let index = scoreControl.selectedSegmentIndex
        config.scoreMin = index == 0 ? nil : index
        let keyword = keywordField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        config.keyword = keyword.isEmpty ? nil : keyword
        onApply?(config)
        dismiss(animated: true)
    }
}
