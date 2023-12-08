# pylint: disable=unused-argument
from typing import Any, Generic, TypeVar
from constructs import Construct
from aws_cdk import (
    Duration,
    Stack,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk.aws_ecs_patterns import (
    ApplicationLoadBalancedFargateService as LBFargateService,
    ApplicationLoadBalancedTaskImageOptions as LBTaskImageOptions,
)
from aws_cdk_lb_fargate.configs import LBFargateConfig, DomainConfig

# pylint: disable=invalid-name
TConfig = TypeVar("TConfig", bound=LBFargateConfig)


class LBFargateStack(Stack, Generic[TConfig]):
    @property
    def _base_name(self) -> str:
        return self.construct_id

    def _name(self, suffix: str = "", prefix: str = "") -> str:
        return "".join(
            [
                part[0].upper() + part[1:]
                for part in f"{prefix} {self._base_name} {suffix}".split()
            ]
        )

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        self.construct_id = construct_id

        super().__init__(scope, self.construct_id, **kwargs)

    def task_image_options(self, config: TConfig) -> LBTaskImageOptions:
        container_image = self.container_image(config)

        return LBTaskImageOptions(
            image=container_image,
            container_port=config.port,
            task_role=self.task_role(config),  # pyright: ignore
            environment=self.image_environment(config),
            secrets=self.image_secrets(config),
        )

    def container_image(
        self, config: TConfig  # pyright: ignore
    ) -> ecs.ContainerImage:
        container_repo = ecr.Repository.from_repository_name(
            self, self._name("Repo"), config.image
        )

        container_image = ecs.ContainerImage.from_ecr_repository(
            container_repo, tag=config.image_tag
        )
        return container_image

    def image_environment(self, config: TConfig) -> dict[str, Any]:
        return {}

    def image_secrets(self, config: TConfig) -> dict[str, Any]:
        return {}

    def task_role(self, config: TConfig) -> iam.Role:
        # Setup role permissions
        return iam.Role(
            self,
            self._name("TaskRole"),
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

    def fargate(self, config: TConfig, vpc: ec2.IVpc) -> LBFargateService:
        #
        # SETUP THE FARGATE SERVICE
        #

        cluster = ecs.Cluster(self, self._name("Cluster"), vpc=vpc)
        load_balancer = self.load_balancer(config, vpc)
        fargate_domain_kwargs, certs = self.configure_domains(
            load_balancer, config.domains
        )

        if config.supports_https:
            protocol = elbv2.ApplicationProtocol.HTTPS
            redirect_http = True
        else:
            protocol = elbv2.ApplicationProtocol.HTTP
            redirect_http = False

        # Create Fargate Service
        fargate = LBFargateService(
            self,
            self._name("FargateService"),
            cluster=cluster,
            open_listener=False,  # Openness is managed in the load balancer
            target_protocol=elbv2.ApplicationProtocol.HTTP,
            load_balancer=load_balancer,
            assign_public_ip=True,
            task_image_options=self.task_image_options(config),
            security_groups=self.security_groups(config, vpc),
            protocol=protocol,
            redirect_http=redirect_http,
            **fargate_domain_kwargs,
        )

        # Without this, the health checks assume HTTPS
        # and cause everything to fail/timeout
        fargate.target_group.configure_health_check(
            protocol=elbv2.Protocol.HTTP,
            port=str(config.port),
            path="/health",
        )

        self.configure_scaling(config, fargate)

        load_balancer.listeners[0].add_certificates(
            self._name("FargateLBCerts"), certificates=certs
        )

        return fargate

    def configure_domains(
        self,
        load_balancer: elbv2.ApplicationLoadBalancer,
        domains: list[DomainConfig],
    ) -> tuple[dict[str, Any], list[acm.Certificate]]:
        certs = []
        fargate_domain_kwargs: dict[str, Any] = {}
        # Setup certificates and zones
        for domain in domains:
            # Retrieve Route53 Alias Record to point to the Load Balancer
            zone_name = self._name(f"{domain.name}HostedZone")
            hosted_zone = route53.HostedZone.from_lookup(
                self, zone_name, domain_name=domain.domain
            )
            route53.ARecord(
                self,
                self._name(f"{domain.name}ARecord"),
                zone=hosted_zone,
                record_name=domain.name,
                target=route53.RecordTarget.from_alias(
                    route53_targets.LoadBalancerTarget(load_balancer)
                ),
            )

            # Setup SSL certificate, if present
            if domain.certificate_arn:
                cert_name = self._name(f"{domain.name}Cert")
                certs.append(
                    acm.Certificate.from_certificate_arn(
                        self, cert_name, domain.certificate_arn
                    )
                )

                # Only bother setting these in the Fargate initialization
                # if it's required. It's only required on HTTPS, so
                # it's easier to only set them when HTTPS is involved
                fargate_domain_kwargs = {
                    "domain_zone": hosted_zone,
                    "certificate": certs[-1],
                    "domain_name": domain.domain,
                }

        return fargate_domain_kwargs, certs

    def configure_scaling(
        self, config: TConfig, fargate: LBFargateService
    ) -> None:
        # Setup AutoScaling policy
        scaling = fargate.service.auto_scale_task_count(
            max_capacity=config.scaling.max_task_count
        )
        scaling.scale_on_cpu_utilization(
            self._name("CpuScaling"),
            target_utilization_percent=config.scaling.target_cpu_util_pct,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )

    def load_balancer(
        self, config: TConfig, vpc: ec2.IVpc
    ) -> elbv2.ApplicationLoadBalancer:
        # Create a Security Group for the Load Balancer
        lb_security_group = ec2.SecurityGroup(
            self, self._name("LBSecurityGroup"), vpc=vpc
        )

        for port in config.external_ports:
            lb_security_group.add_ingress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.tcp(port),
                "Allow http inbound from VPC",
            )

            # If specified, allow access from this IP.
            if config.whitelist_ip:
                lb_security_group.add_ingress_rule(
                    ec2.Peer.ipv4(f"{config.whitelist_ip}/32"),
                    ec2.Port.tcp(port),
                    "developer access",
                )
            # If specified, allow access from the entire internet
            if config.public_access:
                lb_security_group.add_ingress_rule(
                    ec2.Peer.any_ipv4(),
                    ec2.Port.tcp(port),
                    "unrestricted internet access",
                )

        # Create a Application Load Balancer
        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            self._name("LoadBalancer"),
            vpc=vpc,
            internet_facing=True,
            security_group=lb_security_group,
        )

        return load_balancer

    def security_groups(self, config: TConfig, vpc: ec2.IVpc):
        #
        # SECURITY GROUPS AND NETWORKING
        #

        # Setup incoming access
        ingress_sec_group = ec2.SecurityGroup(
            self,
            self._name("FargateIngressSecGroup"),
            vpc=vpc,
            description=(
                "Security group to link to other AWS resource security groups"
            ),
        )
        ingress_sec_group.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(config.port),
            "Allow http inbound from VPC",
        )

        # Setup access to AWS resources
        egress_sec_group = ec2.SecurityGroup(
            self,
            self._name("FargateEgressSecGroup"),
            vpc=vpc,
            description=(
                "Security group to link to other AWS resource security groups"
            ),
        )

        # Give fargate access to all of the ingress configurations
        for idx, ingress in enumerate(config.ingress_confs):
            conf_security_group = ec2.SecurityGroup.from_security_group_id(
                self,
                self._name(f"IngressSecurityGroup{idx}"),
                security_group_id=ingress.security_group_id,
            )
            conf_security_group.add_ingress_rule(
                egress_sec_group,
                ec2.Port.tcp(ingress.port),
            )

        return [ingress_sec_group, egress_sec_group]