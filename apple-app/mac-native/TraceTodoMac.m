#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

@interface TodoQuickRow : NSObject
@property (nonatomic, copy) NSString *memoryId;
@property (nonatomic, copy) NSString *title;
@property (nonatomic, copy) NSString *quadrant;
@property (nonatomic, assign) NSInteger version;
@property (nonatomic, assign) double score;
@end

@implementation TodoQuickRow
@end

@interface AppDelegate : NSObject <NSApplicationDelegate, WKNavigationDelegate, NSTextFieldDelegate>
@property (nonatomic, strong) NSWindow *mainWindow;
@property (nonatomic, strong) WKWebView *webView;
@property (nonatomic, strong) NSStatusItem *statusItem;
@property (nonatomic, strong) NSPopover *popover;
@property (nonatomic, strong) NSTextField *quickInput;
@property (nonatomic, strong) NSStackView *taskStack;
@property (nonatomic, strong) NSTextField *popoverHeaderLabel;
@property (nonatomic, strong) NSTask *backendTask;
@property (nonatomic, strong) NSTimer *pollTimer;
@property (nonatomic, copy) NSString *serverBaseURL;
@property (nonatomic, copy) NSString *spaceId;
@property (nonatomic, copy) NSString *projectRootPath;
@end

@implementation AppDelegate

- (instancetype)init {
    self = [super init];
    if (self) {
        _serverBaseURL = @"http://127.0.0.1:18090";
        _spaceId = @"default";
        NSString *bundlePath = [[NSBundle mainBundle] bundlePath];
        // .../trace-memory/apple-app/dist/TraceTodo.app -> .../trace-memory
        NSString *candRoot = [[[bundlePath stringByDeletingLastPathComponent]
                               stringByDeletingLastPathComponent]
                              stringByDeletingLastPathComponent];
        _projectRootPath = candRoot;
    }
    return self;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [self setupMainMenu];
    [self setupMainWindow];
    [self setupMenuBarItem];
    [self ensureBackendThenLoad];

    self.pollTimer = [NSTimer scheduledTimerWithTimeInterval:12.0
                                                      target:self
                                                    selector:@selector(refreshStatusFromAPI)
                                                    userInfo:nil
                                                     repeats:YES];
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    if (!flag) {
        [self showMainWindow:nil];
    }
    return YES;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    if (self.backendTask && [self.backendTask isRunning]) {
        [self.backendTask terminate];
    }
}

#pragma mark - Auto-Start Backend If Needed

- (void)ensureBackendThenLoad {
    NSURL *healthURL = [NSURL URLWithString:[NSString stringWithFormat:@"%@/api/health?space_id=%@", self.serverBaseURL, self.spaceId]];
    NSURLSessionDataTask *t = [[NSURLSession sharedSession] dataTaskWithURL:healthURL
                                                          completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
        NSHTTPURLResponse *http = (NSHTTPURLResponse *)resp;
        if (!err && http.statusCode == 200) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [self loadWebConsole];
                [self refreshStatusFromAPI];
            });
        } else {
            dispatch_async(dispatch_get_main_queue(), ^{
                [self trySpawnLocalBackend];
            });
        }
    }];
    [t resume];
}

- (void)trySpawnLocalBackend {
    NSString *runWebScript = [self.projectRootPath stringByAppendingPathComponent:@"run_web.py"];
    NSFileManager *fm = [NSFileManager defaultManager];
    if (![fm fileExistsAtPath:runWebScript]) {
        [self refreshStatusFromAPI];
        return;
    }

    NSArray<NSString *> *pythonCandidates = @[
        @"/opt/homebrew/bin/python3",
        @"/usr/local/bin/python3",
        @"/usr/bin/python3"
    ];
    NSString *chosenPy = @"/usr/bin/python3";
    for (NSString *cand in pythonCandidates) {
        if ([fm isExecutableFileAtPath:cand]) {
            chosenPy = cand;
            break;
        }
    }

    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:chosenPy];
    task.arguments = @[runWebScript, @"--lan"];
    task.currentDirectoryURL = [NSURL fileURLWithPath:self.projectRootPath];
    NSError *launchErr = nil;
    if ([task launchAndReturnError:&launchErr]) {
        self.backendTask = task;
    }

    // Retry loading after backend warms up
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.6 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self loadWebConsole];
        [self refreshStatusFromAPI];
    });
}

#pragma mark - Native Menu & Main Window

- (void)setupMainMenu {
    NSMenu *menubar = [[NSMenu alloc] init];
    NSMenuItem *appMenuItem = [[NSMenuItem alloc] init];
    [menubar addItem:appMenuItem];
    [NSApp setMainMenu:menubar];

    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:@"刷新看板" action:@selector(reloadPage:) keyEquivalent:@"r"];
    [appMenu addItemWithTitle:@"显示主窗口" action:@selector(showMainWindow:) keyEquivalent:@"1"];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"退出 有迹" action:@selector(terminate:) keyEquivalent:@"q"];
    [appMenuItem setSubmenu:appMenu];

    // Edit menu so Cmd+C / Cmd+V / Cmd+A work natively inside inputs
    NSMenuItem *editMenuItem = [[NSMenuItem alloc] init];
    [menubar addItem:editMenuItem];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"编辑"];
    [editMenu addItemWithTitle:@"撤销" action:@selector(undo:) keyEquivalent:@"z"];
    [editMenu addItemWithTitle:@"剪切" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"拷贝" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"粘贴" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItemWithTitle:@"全选" action:@selector(selectAll:) keyEquivalent:@"a"];
    [editMenuItem setSubmenu:editMenu];
}

- (void)setupMainWindow {
    NSRect frame = NSMakeRect(0, 0, 1180, 760);
    NSWindowStyleMask style = (NSWindowStyleMaskTitled |
                               NSWindowStyleMaskClosable |
                               NSWindowStyleMaskMiniaturizable |
                               NSWindowStyleMaskResizable);
    self.mainWindow = [[NSWindow alloc] initWithContentRect:frame
                                                  styleMask:style
                                                    backing:NSBackingStoreBuffered
                                                      defer:NO];
    self.mainWindow.title = @"有迹 · 融合记忆与四象限待办中心";
    [self.mainWindow center];
    self.mainWindow.releasedWhenClosed = NO;

    WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
    self.webView = [[WKWebView alloc] initWithFrame:frame configuration:cfg];
    self.webView.navigationDelegate = self;
    self.webView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.mainWindow.contentView = self.webView;

    [self.mainWindow makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)loadWebConsole {
    NSURL *url = [NSURL URLWithString:self.serverBaseURL];
    [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
}

- (void)reloadPage:(id)sender {
    [self.webView reload];
    [self refreshStatusFromAPI];
}

- (void)showMainWindow:(id)sender {
    if (self.popover.isShown) {
        [self.popover performClose:sender];
    }
    [self.mainWindow makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

#pragma mark - Native macOS Menu Bar Status Item & Popover

- (void)setupMenuBarItem {
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    self.statusItem.button.title = @"🎯 待办";
    self.statusItem.button.target = self;
    self.statusItem.button.action = @selector(togglePopover:);

    self.popover = [[NSPopover alloc] init];
    self.popover.behavior = NSPopoverBehaviorTransient;

    NSViewController *vc = [[NSViewController alloc] init];
    NSVisualEffectView *root = [[NSVisualEffectView alloc] initWithFrame:NSMakeRect(0, 0, 360, 330)];
    root.material = NSVisualEffectMaterialPopover;
    root.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    root.state = NSVisualEffectStateActive;

    self.popoverHeaderLabel = [NSTextField labelWithString:@"🧠 有迹 四象限快捷台"];
    self.popoverHeaderLabel.font = [NSFont boldSystemFontOfSize:13.5];
    self.popoverHeaderLabel.frame = NSMakeRect(16, 294, 328, 22);
    [root addSubview:self.popoverHeaderLabel];

    self.quickInput = [[NSTextField alloc] initWithFrame:NSMakeRect(16, 254, 262, 28)];
    self.quickInput.placeholderString = @"输入一句话待办，回车交由 Qwen 抽取...";
    self.quickInput.font = [NSFont systemFontOfSize:12.5];
    self.quickInput.target = self;
    self.quickInput.action = @selector(submitQuickNote:);
    [root addSubview:self.quickInput];

    NSButton *addBtn = [NSButton buttonWithTitle:@"录入" target:self action:@selector(submitQuickNote:)];
    addBtn.bezelStyle = NSBezelStyleRounded;
    addBtn.frame = NSMakeRect(284, 253, 60, 30);
    [root addSubview:addBtn];

    NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:NSMakeRect(16, 48, 328, 194)];
    scroll.drawsBackground = NO;
    scroll.hasVerticalScroller = YES;
    self.taskStack = [[NSStackView alloc] initWithFrame:NSMakeRect(0, 0, 320, 190)];
    self.taskStack.orientation = NSUserInterfaceLayoutOrientationVertical;
    self.taskStack.alignment = NSLayoutAttributeLeading;
    self.taskStack.spacing = 8.0;
    scroll.documentView = self.taskStack;
    [root addSubview:scroll];

    NSButton *openMainBtn = [NSButton buttonWithTitle:@"打开四象限主窗口" target:self action:@selector(showMainWindow:)];
    openMainBtn.frame = NSMakeRect(14, 10, 140, 28);
    [root addSubview:openMainBtn];

    NSButton *refreshBtn = [NSButton buttonWithTitle:@"刷新" target:self action:@selector(refreshStatusFromAPI)];
    refreshBtn.frame = NSMakeRect(220, 10, 60, 28);
    [root addSubview:refreshBtn];

    NSButton *quitBtn = [NSButton buttonWithTitle:@"退出" target:self action:@selector(terminate:)];
    quitBtn.frame = NSMakeRect(284, 10, 60, 28);
    [root addSubview:quitBtn];

    vc.view = root;
    self.popover.contentViewController = vc;
    self.popover.contentSize = NSMakeSize(360, 330);
}

- (void)togglePopover:(id)sender {
    if (self.popover.isShown) {
        [self.popover performClose:sender];
    } else if (self.statusItem.button) {
        [self refreshStatusFromAPI];
        [self.popover showRelativeToRect:self.statusItem.button.bounds
                                  ofView:self.statusItem.button
                           preferredEdge:NSRectEdgeMinY];
    }
}

- (void)submitQuickNote:(id)sender {
    NSString *text = [self.quickInput.stringValue stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (text.length == 0) return;
    self.quickInput.stringValue = @"";
    self.popoverHeaderLabel.stringValue = [NSString stringWithFormat:@"✨ AI 解析中：%@", text];

    NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:@"%@/api/notes/record", self.serverBaseURL]];
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:url];
    req.HTTPMethod = @"POST";
    [req setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
    NSDictionary *body = @{@"content": text, @"space_id": self.spaceId, @"async_mode": @YES};
    req.HTTPBody = [NSJSONSerialization dataWithJSONObject:body options:0 error:nil];

    [[[NSURLSession sharedSession] dataTaskWithRequest:req
                                     completionHandler:^(NSData *d, NSURLResponse *r, NSError *e) {
        if (e || !d) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [self refreshStatusFromAPI];
            });
            return;
        }
        NSDictionary *ack = [NSJSONSerialization JSONObjectWithData:d options:0 error:nil];
        NSString *jobId = ([ack isKindOfClass:[NSDictionary class]] && [ack[@"job_id"] isKindOfClass:[NSString class]]) ? ack[@"job_id"] : @"";
        NSString *waitURLStr = [NSString stringWithFormat:@"%@/api/notes/ingest-status?space_id=%@&job_id=%@&wait_ms=8000",
                                self.serverBaseURL, self.spaceId, jobId];
        NSURL *waitURL = [NSURL URLWithString:waitURLStr];
        [[[NSURLSession sharedSession] dataTaskWithURL:waitURL
                                     completionHandler:^(NSData *wd, NSURLResponse *wr, NSError *we) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [self refreshStatusFromAPI];
                [self.webView evaluateJavaScript:@"if (typeof refreshAfterIngestChange === 'function') { refreshAfterIngestChange(); } else if (typeof loadTasks === 'function') { loadTasks(); }" completionHandler:nil];
            });
        }] resume];
    }] resume];
}

- (void)completeTaskClicked:(NSButton *)sender {
    NSString *payloadStr = sender.identifier; // "memId|version"
    NSArray<NSString *> *parts = [payloadStr componentsSeparatedByString:@"|"];
    if (parts.count < 2) return;
    NSString *memId = parts[0];
    NSInteger ver = [parts[1] integerValue];

    NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:@"%@/api/tasks/transition", self.serverBaseURL]];
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:url];
    req.HTTPMethod = @"POST";
    [req setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
    NSDictionary *body = @{
        @"space_id": self.spaceId,
        @"memory_id": memId,
        @"action": @"complete_task",
        @"expected_version": @(ver)
    };
    req.HTTPBody = [NSJSONSerialization dataWithJSONObject:body options:0 error:nil];

    [[[NSURLSession sharedSession] dataTaskWithRequest:req
                                     completionHandler:^(NSData *d, NSURLResponse *r, NSError *e) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [self refreshStatusFromAPI];
            [self.webView reload];
        });
    }] resume];
}

- (void)refreshStatusFromAPI {
    NSString *u = [NSString stringWithFormat:@"%@/api/tasks/matrix?sort_by=priority&include_done=false&space_id=%@",
                   self.serverBaseURL, self.spaceId];
    NSURL *url = [NSURL URLWithString:u];
    [[[NSURLSession sharedSession] dataTaskWithURL:url
                                 completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
        if (err || !data) {
            dispatch_async(dispatch_get_main_queue(), ^{
                self.statusItem.button.title = @"🎯 离线";
                self.popoverHeaderLabel.stringValue = @"📴 服务端未连接 (点击打开主窗口自动拉起)";
            });
            return;
        }
        NSDictionary *json = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
        if (![json isKindOfClass:[NSDictionary class]]) return;

        NSDictionary *rawCounts = json[@"summary_counts"];
        NSDictionary *counts = [rawCounts isKindOfClass:[NSDictionary class]] ? rawCounts : @{};
        NSInteger totalTodo = [counts[@"total_todo"] respondsToSelector:@selector(integerValue)] ? [counts[@"total_todo"] integerValue] : 0;
        NSInteger q1 = [counts[@"Q1_do_first"] respondsToSelector:@selector(integerValue)] ? [counts[@"Q1_do_first"] integerValue] : 0;
        id rawSorted = json[@"sorted_tasks"];
        NSArray *sorted = [rawSorted isKindOfClass:[NSArray class]] ? rawSorted : @[];

        NSMutableArray<TodoQuickRow *> *rows = [NSMutableArray array];
        for (id rawItem in sorted) {
            if (rows.count >= 7) break;
            if (![rawItem isKindOfClass:[NSDictionary class]]) continue;
            NSDictionary *item = (NSDictionary *)rawItem;
            TodoQuickRow *row = [[TodoQuickRow alloc] init];
            id rawMemId = item[@"memory_id"];
            row.memoryId = [rawMemId isKindOfClass:[NSString class]] ? rawMemId : @"";
            id rawTitle = item[@"title"];
            row.title = [rawTitle isKindOfClass:[NSString class]] ? rawTitle : @"";
            row.version = [item[@"version"] respondsToSelector:@selector(integerValue)] ? [item[@"version"] integerValue] : 1;
            id rawPl = item[@"payload"];
            NSDictionary *pl = [rawPl isKindOfClass:[NSDictionary class]] ? rawPl : @{};
            id rawQuad = pl[@"quadrant"];
            NSString *quadStr = [rawQuad isKindOfClass:[NSString class]] ? rawQuad : @"Q4";
            row.quadrant = quadStr.length >= 2 ? [quadStr substringToIndex:2] : @"Q4";
            row.score = [pl[@"priority_score"] respondsToSelector:@selector(doubleValue)] ? [pl[@"priority_score"] doubleValue] : 0.0;
            [rows addObject:row];
        }

        dispatch_async(dispatch_get_main_queue(), ^{
            NSString *badgeIcon = q1 > 0 ? @"🔥" : @"🎯";
            self.statusItem.button.title = [NSString stringWithFormat:@"%@ %ld", badgeIcon, (long)totalTodo];
            self.popoverHeaderLabel.stringValue = [NSString stringWithFormat:@"🧠 待办: %ld 项 · Q1 紧急重要: %ld 项", (long)totalTodo, (long)q1];

            for (NSView *sub in [self.taskStack.arrangedSubviews copy]) {
                [self.taskStack removeArrangedSubview:sub];
                [sub removeFromSuperview];
            }
            if (rows.count == 0) {
                NSTextField *empty = [NSTextField labelWithString:@"🎉 当前没有未完成待办"];
                empty.textColor = [NSColor secondaryLabelColor];
                [self.taskStack addArrangedSubview:empty];
            } else {
                for (TodoQuickRow *r in rows) {
                    NSString *btnTitle = [NSString stringWithFormat:@"[%@] %@", r.quadrant, r.title];
                    NSButton *chk = [NSButton checkboxWithTitle:btnTitle target:self action:@selector(completeTaskClicked:)];
                    chk.identifier = [NSString stringWithFormat:@"%@|%ld", r.memoryId, (long)r.version];
                    chk.font = [NSFont systemFontOfSize:12.5];
                    chk.lineBreakMode = NSLineBreakByTruncatingTail;
                    [self.taskStack addArrangedSubview:chk];
                }
            }
        });
    }] resume];
}

@end

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        AppDelegate *delegate = [[AppDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
