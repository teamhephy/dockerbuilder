import docker
import json
import os
import sys
import tarfile
import time
import requests
import subprocess

DEBUG = os.environ.get('DEIS_DEBUG') in ('true', '1')
registryLocation = os.getenv('DEIS_REGISTRY_LOCATION', 'on-cluster')


def log_output(stream, decode):
    error = False
    for chunk in stream:
        if isinstance(chunk, bytes):
            # Convert to dict, since docker-py returns some errors as raw bytes.
            chunk = eval(chunk)
        if 'error' in chunk:
            error = True
            print(chunk['error'])
        elif decode:
            stream_chunk = chunk.get('stream')
            if stream_chunk:
                # Must handle chunks as bytes to avoid UnicodeEncodeError.
                encoded_chunk = stream_chunk.encode('utf-8')
                sys.stdout.buffer.write(encoded_chunk)
        elif DEBUG:
            print(chunk)
        sys.stdout.flush()
    if error:
        # HACK: delay so stderr is logged before this dockerbuilder pod exits.
        time.sleep(3)
        exit(1)


def log(msg):
    if DEBUG:
        print(msg)


def get_registry_name():
    hostname = os.getenv('DEIS_REGISTRY_HOSTNAME', "")
    hostname = hostname.replace("https://", "").replace("http://", "")
    if registryLocation == "off-cluster":
        organization = os.getenv('DEIS_REGISTRY_ORGANIZATION')
        regName = ""
        # empty hostname means dockerhub and hence no need to prefix the image
        if hostname != "":
            regName = hostname + "/"
        # Registries may have organizations/namespaces under them which needs to
        # be prefixed to the image
        if organization != "":
            regName = regName + organization
        return regName
    elif registryLocation == "ecr":
        return hostname
    elif registryLocation == "gcr":
        return hostname + "/" + os.getenv('DEIS_REGISTRY_GCS_PROJ_ID')
    else:
        return "{}:{}".format(os.getenv("DEIS_REGISTRY_SERVICE_HOST"),
                              os.getenv("DEIS_REGISTRY_SERVICE_PORT"))


def download_file(tar_path):
    os.putenv('BUCKET_FILE', "/var/run/secrets/deis/objectstore/creds/builder-bucket")
    if os.getenv('BUILDER_STORAGE') == "minio":
        os.makedirs("/tmp/objectstore/minio")
        bucketFile = open('/tmp/objectstore/minio/builder-bucket', 'w')
        bucketFile.write('git')
        bucketFile.close()
        os.putenv('BUCKET_FILE', "/tmp/objectstore/minio/builder-bucket")
    elif os.getenv('BUILDER_STORAGE') in ["azure", "swift"]:
        os.putenv('CONTAINER_FILE', "/var/run/secrets/deis/objectstore/creds/builder-container")
    command = [
        "objstorage",
        "--storage-type="+os.getenv('BUILDER_STORAGE'),
        "download",
        tar_path,
        "apptar"
    ]
    subprocess.check_call(command)

def sanitize_env():
    # Remove any environment variables that can affect the AWS SDK or the CLI
    # See https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html
    sensitive_vars = [
        'AWS_ACCESS_KEY_ID',
        'AWS_CA_BUNDLE',
        'AWS_CLI_AUTO_PROMPT',
        'AWS_CLI_FILE_ENCODING',
        'AWS_CONFIG_FILE',
        'AWS_DATA_PATH',
        'AWS_DEFAULT_OUTPUT',
        'AWS_DEFAULT_REGION',
        'AWS_EC2_METADATA_DISABLED',
        'AWS_MAX_ATTEMPTS',
        'AWS_METADATA_SERVICE_NUM_ATTEMPTS',
        'AWS_METADATA_SERVICE_TIMEOUT',
        'AWS_PAGER',
        'AWS_PROFILE',
        'AWS_REGION',
        'AWS_RETRY_MODE',
        'AWS_ROLE_ARN',
        'AWS_ROLE_SESSION_NAME',
        'AWS_SECRET_ACCESS_KEY',
        'AWS_SESSION_TOKEN',
        'AWS_SHARED_CREDENTIALS_FILE',
        'AWS_STS_REGIONAL_ENDPOINTS',
        'AWS_WEB_IDENTITY_TOKEN_FILE'
    ]
    for var in sensitive_vars
        os.environ.pop(var, None)

tar_path = os.getenv('TAR_PATH')
if tar_path:
    if os.path.exists("/var/run/secrets/deis/objectstore/creds/"):
        sanitize_env()
        download_file(tar_path)
    else:
        r = requests.get(tar_path)
        with open("apptar", "wb") as app:
            app.write(r.content)

log("download tar file complete")
with tarfile.open("apptar", "r:gz") as tar:
    tar.extractall("/app/")
log("extracting tar file complete")
buildargs = json.loads(os.getenv('DOCKER_BUILD_ARGS', '{}'))
# inject docker build args into the Dockerfile so we get around Dockerfiles that don't have things
# like PORT defined.
with open("/app/Dockerfile", "a") as dockerfile:
    # ensure we are on a new line
    dockerfile.write("\n")
    for envvar in buildargs:
        dockerfile.write("ARG {}\n".format(envvar))
client = docker.Client(version='auto')
if registryLocation != "on-cluster":
    registry = os.getenv('DEIS_REGISTRY_HOSTNAME', 'https://index.docker.io/v1/')
    username = os.getenv('DEIS_REGISTRY_USERNAME')
    password = os.getenv('DEIS_REGISTRY_PASSWORD')
    client.login(username=username, password=password, registry=registry)
registry = get_registry_name()
imageName, imageTag = os.getenv('IMG_NAME').split(":", 1)
repo = registry + "/" + os.getenv('IMG_NAME')
stream = client.build(
    tag=repo,
    stream=True,
    decode=True,
    rm=True,
    pull=True,
    path='/app',
    buildargs=buildargs)
log_output(stream, True)
print("Pushing to registry")
stream = client.push(registry+'/'+imageName, tag=imageTag, stream=True)
log_output(stream, False)
